"""Conservative Takeout merge. Originals are read; new files are never overwritten."""
import csv
import ctypes
import errno
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat as stat_module
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from .database import archive, connect, now, photo_values, save
from .scanner import Counts, Display, EXTENSIONS, scan, validate_root

log = logging.getLogger(__name__)
LEDGER = '''CREATE TABLE IF NOT EXISTS takeout_imports (
    id INTEGER PRIMARY KEY,
    archive_id TEXT NOT NULL REFERENCES archives(id),
    source_root TEXT NOT NULL,
    source_relative TEXT NOT NULL,
    source_size INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    destination_relative TEXT NOT NULL,
    temporary_relative TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','complete')),
    photo_id INTEGER REFERENCES photos(id),
    imported_at TEXT,
    UNIQUE(archive_id, source_root, source_relative, source_size, source_mtime_ns)
)'''


@dataclass
class ImportCounts(Counts):
    uncertain: int = 0
    registered: int = 0
    renamed: int = 0
    resumed: int = 0
    recovered: int = 0
    sidecars: int = 0
    bytes_copied: int = 0
    planned: int = 0


class ImportDisplay(Display):
    def render(self):
        c = self.counts
        self.progress.update(self.task, completed=c.processed)
        elapsed = time.monotonic() - self.started
        rate = c.processed / max(time.monotonic() - self.catalogue_started, .001) if self.catalogue_started else 0
        text = Text(f'{c.stage} | Directories: {c.directories:,} | Images: {c.discovered:,}\n'
                    f'Copied: {c.catalogued:,} | Planned: {c.planned:,} | Duplicates: {c.skipped:,} | Already imported: {c.resumed:,}\n'
                    f'Uncertain: {c.uncertain:,} | Renamed: {c.renamed:,} | Errors: {c.errors:,} | Copied bytes: {c.bytes_copied:,}\n'
                    f'Rate: {rate:.1f} images/s | Elapsed: {elapsed:.1f}s\nCurrent: {c.current}')
        return Panel(Group(text, self.progress) if self.catalogue_started else text, title='Takeout import')


def signature(row):
    """A heuristic EXIF identity, never a byte/content identity."""
    capture = row['captured_utc'] or row['captured_local']
    make, model = row['camera_make'], row['camera_model']
    if not capture or not make or not model or not row['width'] or not row['height']:
        return None
    sub = row['subsecond_original']
    if sub is not None and not str(sub).strip().isdigit():
        return None
    # Preserve EXIF precision beyond normalized SQLite microseconds.
    sub = str(sub).strip().rstrip('0') if sub is not None else None
    make, model = ' '.join(str(make).casefold().split()), ' '.join(str(model).casefold().split())
    try:
        width, height = int(row['width']), int(row['height'])
    except (TypeError, ValueError, OverflowError):
        return None
    if not make or not model or width <= 0 or height <= 0:
        return None
    key = ['utc' if row['captured_utc'] else 'local', capture, sub, make, model, width, height]
    return json.dumps(key, ensure_ascii=False)


def safe_directory(root, relative, create=False):
    path = root
    for part in Path(relative).parts:
        if part in ('..', '/', ''):
            raise ValueError('Invalid destination directory')
        path = path / part
        if path.is_symlink():
            raise ValueError(f'Destination directory is a symlink: {path}')
        if create:
            path.mkdir(exist_ok=True)
        elif path.exists() and not path.is_dir():
            raise ValueError(f'Destination is not a directory: {path}')
    return path


def equal_files(left, right):
    """Bounded byte comparison for EXIF candidates, collisions and recovery."""
    if right.is_symlink() or not right.is_file() or left.stat().st_size != right.stat().st_size:
        return False
    with left.open('rb') as a, right.open('rb') as b:
        before_a, before_b = os.fstat(a.fileno()), os.fstat(b.fileno())
        while True:
            one, two = a.read(1024 * 1024), b.read(1024 * 1024)
            if one != two:
                return False
            if not one:
                after_a, after_b = os.fstat(a.fileno()), os.fstat(b.fileno())
                return ((before_a.st_size, before_a.st_mtime_ns) == (after_a.st_size, after_a.st_mtime_ns)
                        and (before_b.st_size, before_b.st_mtime_ns) == (after_b.st_size, after_b.st_mtime_ns))


def publish(temporary, destination):
    """Atomic Linux rename without replacement; fallback never overwrites either."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename:
        result = rename(-100, ctypes.c_char_p(os.fsencode(temporary)),
                        -100, ctypes.c_char_p(os.fsencode(destination)), 1)
        if result == 0:
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(code, os.strerror(code), str(destination))
    os.link(temporary, destination)
    temporary.unlink()


def copy_atomic(source, destination, temporary, update=None, expected=None):
    created = False
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as original, temporary.open('xb') as output:
            created = True
            before = os.fstat(original.fileno())
            if not stat_module.S_ISREG(before.st_mode):
                raise ValueError('Source is not a regular file')
            if expected and (before.st_size, before.st_mtime_ns) != expected:
                raise OSError('Source changed since discovery; retry the import')
            while chunk := original.read(1024 * 1024):
                output.write(chunk)
                if update:
                    update(len(chunk))
            after = os.fstat(original.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OSError('Source changed during copying; retry the import')
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, temporary, follow_symlinks=False)
        publish(temporary, destination)
    finally:
        # Never remove a pre-existing file if exclusive temporary creation failed.
        if created:
            temporary.unlink(missing_ok=True)


def destination_name(queue, root, source, source_relative):
    directory = safe_directory(root, Path('takeout') / source.parent.name)
    candidate = directory / source.name
    digest = hashlib.sha256(source_relative.encode()).hexdigest()[:12]
    number = 0
    while True:
        relative = str(candidate.relative_to(root))
        reserved = queue.execute('SELECT source FROM reserved WHERE path=?', (relative,)).fetchone()
        if not reserved and not candidate.exists() and not candidate.is_symlink():
            queue.execute('INSERT INTO reserved VALUES (?,?)', (relative, str(source)))
            return candidate, number > 0, False
        comparison = Path(reserved[0]) if reserved else candidate
        if equal_files(source, comparison):
            if not reserved:
                queue.execute('INSERT INTO reserved VALUES (?,?)', (relative, str(source)))
            return candidate, number > 0, True
        number += 1
        tail = f'__{digest}' + (f'_{number}' if number > 1 else '') + source.suffix
        # Leave room on byte-limited filesystems, including long Unicode names.
        stem = source.stem.encode()[:max(1, 240 - len(tail.encode()))].decode(errors='ignore')
        candidate = directory / (stem + tail)


def records_for(reader, paths):
    try:
        return reader.read(paths)
    except Exception as exc:
        log.warning('Takeout metadata batch failed, retrying individually: %s', exc)
        records = {}
        for path in paths:
            try:
                records.update(reader.read([path]))
            except Exception as failure:
                records[path] = {'Error': str(failure)}
        return records


def discover(queue, source, counts, display, report):
    queue.execute('CREATE TABLE directories(path TEXT)')
    queue.execute('CREATE TABLE candidates(path TEXT,relative TEXT,size INTEGER,mtime INTEGER)')
    queue.execute('CREATE TABLE reserved(path TEXT PRIMARY KEY,source TEXT)')
    queue.execute('INSERT INTO directories VALUES (?)', (str(source),))
    visited = 0
    while row := queue.execute('SELECT rowid,path FROM directories WHERE rowid>? ORDER BY rowid LIMIT 1', (visited,)).fetchone():
        visited, directory = row
        counts.directories += 1
        counts.current = directory
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            queue.execute('INSERT INTO directories VALUES (?)', (entry.path,))
                        elif Path(entry.name).suffix.lower() in EXTENSIONS and entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            queue.execute('INSERT INTO candidates VALUES (?,?,?,?)',
                                          (entry.path, str(Path(entry.path).relative_to(source)), stat.st_size, stat.st_mtime_ns))
                            counts.discovered += 1
                        display.update()
                    except OSError as exc:
                        counts.errors += 1
                        log.error('Takeout discovery: %s: %s', entry.path, exc)
                        report.writerow(['error', entry.path, '', str(exc), ''])
        except OSError as exc:
            counts.errors += 1
            log.error('Takeout discovery: %s: %s', directory, exc)
            report.writerow(['error', directory, '', str(exc), ''])
        queue.commit()
        display.update()


def build_matches(queue, db, identity, counts, display):
    queue.execute('CREATE TABLE matches(signature TEXT,path TEXT,size INTEGER,mtime INTEGER,photo_id INTEGER,planned INTEGER DEFAULT 0)')
    cursor = db.execute('SELECT * FROM photos WHERE archive_id=?', (identity,))
    while batch := cursor.fetchmany(256):
        entries = []
        for row in batch:
            key = signature(row)
            if key:
                entries.append((key, row['full_path'], row['file_size'], row['mtime_ns'], row['id']))
        queue.executemany('INSERT INTO matches(signature,path,size,mtime,photo_id) VALUES (?,?,?,?,?)', entries)
        queue.commit()
        display.update()
    queue.execute('CREATE INDEX matches_signature ON matches(signature)')


def duplicate(queue, key, root, source):
    if not key:
        return None
    for row in queue.execute('SELECT path,size,mtime,photo_id,planned FROM matches WHERE signature=?', (key,)):
        path, size, mtime, photo_id, planned = row
        try:
            candidate = Path(path)
            if candidate.is_symlink():
                continue
            # Dry-run entries point to the original source bytes, since no
            # destination image has been written. Never deduplicate on EXIF alone.
            if not planned and not candidate.resolve(strict=True).is_relative_to(root):
                continue
            stat = candidate.stat()
            if (stat.st_size, stat.st_mtime_ns) != (size, mtime):
                continue
            if equal_files(source, candidate):
                after = candidate.stat()
                if (after.st_size, after.st_mtime_ns) == (size, mtime):
                    return photo_id or 'planned'
        except OSError:
            # An unreadable or changed candidate cannot justify discarding a source.
            continue
    return None


def copy_sidecars(source, destination, root, dry_run, counts, report):
    # Exact, unambiguous image-name associations only. Never guess truncated names.
    for suffix in ('.json', '.supplemental-metadata.json'):
        sidecar = source.with_name(source.name + suffix)
        target = destination.with_name(destination.name + suffix)
        try:
            if sidecar.is_symlink() or not sidecar.is_file():
                continue
            if target.exists() or target.is_symlink():
                if not equal_files(sidecar, target):
                    raise FileExistsError(f'Conflicting sidecar preserved: {target}')
                continue
            if not dry_run:
                temporary = target.parent / ('.popeye-sidecar-' + uuid.uuid4().hex + '.part')
                copy_atomic(sidecar, target, temporary)
            counts.sidecars += 1
            report.writerow(['sidecar-planned' if dry_run else 'sidecar-copied', str(sidecar), str(target), 'Associated JSON preserved', ''])
        except OSError as exc:
            if exc.errno == errno.ENAMETOOLONG:
                # Takeout may truncate sidecar names; do not guess their association.
                log.warning('Cannot associate a long-name Takeout sidecar: %s', sidecar)
                continue
            counts.errors += 1
            log.error('Takeout sidecar: %s: %s', sidecar, exc)
            report.writerow(['error', str(sidecar), str(target), str(exc), ''])


def summary(counts, dry_run, report_path):
    print(f'\nTakeout {"dry-run" if dry_run else "import"} summary')
    print(f'Directories: {counts.directories:,} | Discovered: {counts.discovered:,} | Processed: {counts.processed:,}')
    print(f'Copied: {counts.catalogued:,} | Would copy: {counts.planned:,} | Confirmed duplicates skipped: {counts.skipped:,}')
    print(f'Already imported: {counts.resumed:,} | Recovered/catalogued: {counts.recovered:,} | Uncertain metadata: {counts.uncertain:,}')
    print(f'Collision renames: {counts.renamed:,} | JSON sidecars {"planned" if dry_run else "copied"}: {counts.sidecars:,}')
    print(f'Registered in SQLite: {counts.registered:,}')
    print(f'Copied bytes: {counts.bytes_copied:,} | Errors: {counts.errors:,}\nReport: {report_path}')


def import_takeout(db, source, root, reader, report_path, dry_run=False,
                   batch_size=64, show_progress=True, mount_point=None, refresh=True):
    source = validate_root(source)
    root = validate_root(root, mount_point)
    if source.is_relative_to(root) or root.is_relative_to(source):
        raise ValueError('Source and destination archives must not overlap or contain one another')
    if not 1 <= batch_size <= 256:
        raise ValueError('Batch size must be between 1 and 256')
    report_path = Path(report_path).resolve()
    if report_path.is_relative_to(root) or report_path.is_relative_to(source):
        raise ValueError('The report must be outside both the source and destination photo trees')
    if not dry_run and (os.statvfs(root).f_flag & os.ST_RDONLY or not os.access(root, os.W_OK)):
        raise ValueError('Destination archive is read-only; mount it writable before importing (dry-run is permitted)')
    counts = ImportCounts()
    with tempfile.TemporaryDirectory(prefix='takeout-import-') as temporary:
        # A dry-run refreshes a database snapshot, never the user's catalogue.
        snapshot = None
        working = db
        try:
            if dry_run:
                snapshot_path = Path(temporary) / 'snapshot.sqlite3'
                with closing(sqlite3.connect(snapshot_path)) as copy:
                    db.backup(copy)
                snapshot = connect(snapshot_path)
                working = snapshot
            # Exclusive report creation prevents inadvertently replacing an older report.
            with report_path.open('x', newline='', encoding='utf-8') as report_file:
                report = csv.writer(report_file)
                report.writerow(['action', 'source', 'destination', 'reason', 'matched_photo_id'])
                try:
                    if refresh:
                        print('Refreshing the local catalogue before matching Takeout images…')
                        refreshed = scan(working, root, reader, batch_size, show_progress, mount_point)
                        if refreshed.errors:
                            counts.errors += refreshed.errors
                            raise ValueError(f'Local refresh had {refreshed.errors} errors. Resolve them before merging; see the log')
                    identity = archive(working, root)
                    with working:
                        working.execute(LEDGER)
                    with closing(sqlite3.connect(Path(temporary) / 'queue.sqlite3')) as queue:
                        with ImportDisplay(counts, show_progress) as display:
                            counts.stage = 'Discovering Takeout images'
                            discover(queue, source, counts, display, report)
                            counts.stage = 'Indexing local EXIF metadata'
                            display.update()
                            build_matches(queue, working, identity, counts, display)
                            counts.stage = 'Comparing (dry-run)' if dry_run else 'Comparing and copying'
                            display.catalogue_started = time.monotonic()
                            display.progress.update(display.task, total=counts.discovered)
                            cursor = queue.execute('SELECT path,relative,size,mtime FROM candidates')
                            while batch := cursor.fetchmany(batch_size):
                                validate_root(root, mount_point)
                                records = records_for(reader, [row[0] for row in batch])
                                for path, relative, size, mtime in batch:
                                    item = Path(path)
                                    destination = None
                                    copied = False
                                    counts.current = str(item)
                                    try:
                                        record = records.get(path)
                                        if record is None or record.get('Error'):
                                            raise ValueError((record or {}).get('Error', 'No metadata result returned'))
                                        if record.get('Warning'):
                                            log.warning('Takeout metadata: %s: %s', path, record['Warning'])
                                        current = item.stat(follow_symlinks=False)
                                        if item.is_symlink() or not item.resolve(strict=True).is_relative_to(source):
                                            raise ValueError('Source became a symlink or left the source folder')
                                        if (current.st_size, current.st_mtime_ns) != (size, mtime):
                                            raise OSError('Source changed since discovery; retry the import')
                                        values = photo_values(identity, source, item, record, current)
                                        key = signature(values)
                                        fingerprint = (identity, str(source), relative, size, mtime)
                                        ledger = working.execute('SELECT * FROM takeout_imports WHERE archive_id=? AND source_root=? '
                                                                 'AND source_relative=? AND source_size=? AND source_mtime_ns=?', fingerprint).fetchone()
                                        if ledger:
                                            if not dry_run:
                                                clean_temporary(root, ledger['temporary_relative'])
                                            destination = safe_directory(root, Path(ledger['destination_relative']).parent) / Path(ledger['destination_relative']).name
                                            if ledger['status'] == 'complete':
                                                saved = working.execute('SELECT * FROM photos WHERE id=?', (ledger['photo_id'],)).fetchone()
                                                if saved and destination.is_file() and not destination.is_symlink():
                                                    stat = destination.stat()
                                                    if (stat.st_size, stat.st_mtime_ns) == (saved['file_size'], saved['mtime_ns']):
                                                        counts.resumed += 1
                                                        report.writerow(['already-imported', path, str(destination), 'Completed source fingerprint', ledger['photo_id']])
                                                        copy_sidecars(item, destination, root, dry_run, counts, report)
                                                        continue
                                            # A published file with an uncommitted DB row is safely recovered.
                                            if destination.exists() and equal_files(item, destination):
                                                if not dry_run:
                                                    register_copy(working, identity, root, destination, record, ledger['id'])
                                                    counts.registered += 1
                                                counts.recovered += 1
                                                report.writerow(['would-recover' if dry_run else 'recovered', path, str(destination), 'Existing destination is byte-identical', ''])
                                                add_match(queue, key, destination, None, dry_run, item)
                                                copy_sidecars(item, destination, root, dry_run, counts, report)
                                                continue
                                        matched = duplicate(queue, key, root, item)
                                        if matched is not None:
                                            counts.skipped += 1
                                            report.writerow(['duplicate', path, '', 'EXIF candidate confirmed byte-identical', matched])
                                            continue
                                        if key is None:
                                            counts.uncertain += 1
                                        destination, renamed, identical = destination_name(queue, root, item, relative)
                                        counts.renamed += int(renamed)
                                        reason = 'Insufficient EXIF; retained conservatively' if key is None else 'No byte-identical EXIF candidate in local archive'
                                        if dry_run:
                                            if identical:
                                                counts.recovered += 1
                                            else:
                                                counts.planned += 1
                                            add_match(queue, key, destination, None, True, item)
                                            report.writerow(['would-catalogue' if identical else 'would-copy', path, str(destination), reason, ''])
                                            copy_sidecars(item, destination, root, True, counts, report)
                                            continue
                                        validate_root(root, mount_point)
                                        safe_directory(root, destination.parent.relative_to(root), create=True)
                                        temp_path = destination.parent / ('.popeye-import-' + uuid.uuid4().hex + '.part')
                                        # Journal before writing any image bytes: recovery survives a process crash.
                                        with working:
                                            working.execute('INSERT INTO takeout_imports(archive_id,source_root,source_relative,source_size,source_mtime_ns,'
                                                            'destination_relative,temporary_relative,status) VALUES (?,?,?,?,?,?,?,\'pending\') '
                                                            'ON CONFLICT(archive_id,source_root,source_relative,source_size,source_mtime_ns) DO UPDATE SET '
                                                            'destination_relative=excluded.destination_relative,temporary_relative=excluded.temporary_relative,status=\'pending\'',
                                                            (*fingerprint, str(destination.relative_to(root)), str(temp_path.relative_to(root))))
                                        job_id = working.execute('SELECT id FROM takeout_imports WHERE archive_id=? AND source_root=? AND source_relative=? '
                                                                 'AND source_size=? AND source_mtime_ns=?', fingerprint).fetchone()[0]
                                        if destination.exists():
                                            if not equal_files(item, destination):
                                                raise FileExistsError(f'Destination changed during planning: {destination}')
                                            counts.recovered += 1
                                        else:
                                            def transferred(amount):
                                                display.update()
                                            copy_atomic(item, destination, temp_path, transferred, (size, mtime))
                                            copied = True
                                            counts.catalogued += 1
                                            counts.bytes_copied += size
                                        photo_id = register_copy(working, identity, root, destination, record, job_id)
                                        counts.registered += 1
                                        add_match(queue, key, destination, photo_id, False)
                                        copy_sidecars(item, destination, root, False, counts, report)
                                        report.writerow(['copied' if copied else 'catalogued', path, str(destination), reason, photo_id])
                                        log.info('Takeout imported %s -> %s (photo %s)', path, destination, photo_id)
                                    except (OSError, ValueError, sqlite3.Error) as exc:
                                        counts.errors += 1
                                        report.writerow(['error', path, str(destination or ''), str(exc), ''])
                                        log.error('Takeout import: %s: %s', path, exc)
                                        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EROFS, errno.ENODEV, errno.EIO):
                                            raise
                                    finally:
                                        counts.processed += 1
                                        report_file.flush()
                                        display.update()
                                queue.commit()
                finally:
                    summary(counts, dry_run, report_path)
        finally:
            if snapshot:
                snapshot.close()
    return counts


def add_match(queue, key, destination, photo_id, planned, comparison_source=None):
    if key:
        comparison = comparison_source if planned else destination
        stat = comparison.stat()
        queue.execute('INSERT INTO matches VALUES (?,?,?,?,?,?)',
                      (key, str(comparison), stat.st_size, stat.st_mtime_ns, photo_id, int(planned)))


def register_copy(db, identity, root, destination, record, job_id):
    with db:
        save(db, photo_values(identity, root, destination, record, destination.stat()))
        photo_id = db.execute('SELECT id FROM photos WHERE archive_id=? AND relative_path=?',
                              (identity, str(destination.relative_to(root)))).fetchone()[0]
        db.execute("UPDATE takeout_imports SET status='complete',photo_id=?,imported_at=?,temporary_relative=NULL WHERE id=?",
                   (photo_id, now(), job_id))
    return photo_id


def clean_temporary(root, relative):
    if not relative:
        return
    path = Path(relative)
    name = path.name
    token = name.removeprefix('.popeye-import-').removesuffix('.part')
    if path.is_absolute() or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Invalid import temporary filename in journal')
    directory = safe_directory(root, path.parent)
    (directory / name).unlink(missing_ok=True)
