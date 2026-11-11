import logging
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskProgressColumn, TimeRemainingColumn
from rich.text import Text
from rich.console import Group

from .database import archive, now, save, stable_id
from .metadata import timestamps

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.heif', '.webp'}
log = logging.getLogger(__name__)


@dataclass
class Counts:
    directories: int = 0
    discovered: int = 0
    catalogued: int = 0
    skipped: int = 0
    processed: int = 0
    errors: int = 0
    current: str = ''
    stage: str = 'Discovery'


class Display:
    def __init__(self, counts, enabled):
        self.counts = counts
        self.started = time.monotonic()
        self.last_refresh = 0
        self.catalogue_started = None
        self.progress = Progress(BarColumn(), TaskProgressColumn(), TimeRemainingColumn())
        self.task = self.progress.add_task('Catalogue', total=None)
        self.live = Live(self.render(), refresh_per_second=4, auto_refresh=False) if enabled else None

    def render(self):
        c = self.counts
        elapsed = time.monotonic() - self.started
        done = c.processed
        rate = done / max(time.monotonic() - self.catalogue_started, .001) if self.catalogue_started else 0
        self.progress.update(self.task, completed=done)
        text = Text(f'{c.stage} | Directories: {c.directories:,} | Discovered: {c.discovered:,}\n'
                    f'Catalogued: {c.catalogued:,} | Unchanged: {c.skipped:,} | Errors: {c.errors:,}\n'
                    f'Rate: {rate:.1f} photos/s | Elapsed: {elapsed:.1f}s\nCurrent: {c.current}')
        return Panel(Group(text, self.progress) if self.catalogue_started else text,
                     title='Local Photo Catalogue')

    def update(self):
        if time.monotonic() - self.last_refresh < .2:
            return
        self.last_refresh = time.monotonic()
        if self.live:
            self.live.update(self.render(), refresh=True)

    def __enter__(self):
        if self.live:
            self.live.start()
        return self

    def __exit__(self, *args):
        if self.live:
            self.live.update(self.render(), refresh=True)
            self.live.stop()


def validate_root(root, mount_point=None):
    root = Path(root).absolute()
    if root.is_symlink():
        raise ValueError('The archive root must not be a symbolic link')
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Photo root must be a directory')
    # A disconnected USB mount can leave an ordinary empty directory behind.
    if mount_point:
        mount = Path(mount_point).resolve(strict=True)
        if not mount.is_mount() or not root.is_relative_to(mount):
            raise ValueError('Expected mount point is not mounted or does not contain the archive')
    elif root.parts[1:2] in [('media',), ('mnt',), ('run',)]:
        # /run is commonly tmpfs; it does not prove a USB volume is mounted.
        generic = {Path('/'), Path('/run'), Path('/run/media'), Path('/media'), Path('/mnt')}
        if not any(p.is_mount() for p in (root, *root.parents) if p not in generic):
            raise ValueError('No mounted source volume found; connect/mount the USB disk first')
    if not os.access(root, os.R_OK | os.X_OK):
        raise ValueError('Photo root is not readable')
    return root


def scan(db, root, reader, batch_size=64, show_progress=True, mount_point=None):
    root = validate_root(root, mount_point)
    if batch_size < 1 or batch_size > 256:
        raise ValueError('Batch size must be between 1 and 256')
    identity = archive(db, root)
    counts = Counts()

    def error(path, exc):
        counts.errors += 1
        log.error('%s: %s', path, exc)

    # Temporary SQLite queue provides bounded Python memory even for huge archives.
    with tempfile.TemporaryDirectory(prefix='photo-discovery-') as temporary:
        queue = sqlite3.connect(str(Path(temporary) / 'queue.db'))
        try:
            queue.execute('CREATE TABLE candidates (path TEXT, relative TEXT, size INTEGER, mtime INTEGER)')
            with Display(counts, show_progress) as display:
                # Iterative directory walk stored on disk, avoiding os.walk's per-directory file list.
                queue.execute('CREATE TABLE directories (path TEXT)')
                queue.execute('INSERT INTO directories VALUES (?)', (str(root),))
                visited = 0
                while True:
                    row = queue.execute('SELECT rowid,path FROM directories WHERE rowid>? ORDER BY rowid LIMIT 1',
                                        (visited,)).fetchone()
                    if row is None:
                        break
                    visited, directory = row
                    counts.current = directory
                    counts.directories += 1
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
                                                      (entry.path, os.path.relpath(entry.path, root), stat.st_size, stat.st_mtime_ns))
                                        counts.discovered += 1
                                    if counts.discovered % 256 == 0:
                                        display.update()
                                except OSError as exc:
                                    error(entry.path, exc)
                    except OSError as exc:
                        error(directory, exc)
                    queue.commit()
                    display.update()
                counts.stage = 'Cataloguing'
                display.catalogue_started = time.monotonic()
                display.progress.update(display.task, total=counts.discovered)
                cursor = queue.execute('SELECT path,relative,size,mtime FROM candidates')
                while batch := cursor.fetchmany(batch_size):
                    # Check the mount before each batch; never prune missing entries.
                    validate_root(root, mount_point)
                    pending = []
                    scanned = now()
                    with db:
                        for path, relative, size, mtime in batch:
                            counts.current = str(Path(path).parent)
                            existing = db.execute('SELECT file_size,mtime_ns FROM photos WHERE archive_id=? AND relative_path=?',
                                                  (identity, relative)).fetchone()
                            if existing and (existing['file_size'], existing['mtime_ns']) == (size, mtime):
                                db.execute('UPDATE photos SET last_scanned_at=? WHERE archive_id=? AND relative_path=?',
                                           (scanned, identity, relative))
                                counts.skipped += 1
                            else:
                                pending.append((path, relative, size, mtime))
                    if pending:
                        try:
                            records = reader.read([p[0] for p in pending])
                        except Exception as exc:
                            # Retry individually to isolate a bad file or a failed batch.
                            log.warning('Metadata batch failed, retrying individual files: %s', exc)
                            records = {}
                            for item in pending:
                                try:
                                    records.update(reader.read([item[0]]))
                                except Exception as retry_exc:
                                    records[item[0]] = {'Error': str(retry_exc)}
                        # Commit each completed file on interruption, otherwise one transaction per batch.
                        try:
                            for path, relative, size, mtime in pending:
                                record = records.get(path)
                                if record is None:
                                    error(path, 'No metadata result; file will be retried next scan')
                                    continue
                                if record.get('Error'):
                                    error(path, record['Error'])
                                    continue
                                if record.get('Warning'):
                                    log.warning('%s: %s', path, record['Warning'])
                                try:
                                    stat = os.stat(path, follow_symlinks=False)
                                    if os.path.islink(path) or (stat.st_size, stat.st_mtime_ns) != (size, mtime):
                                        raise OSError('File changed since discovery; retry next scan')
                                    local, utc = timestamps(record.get('DateTimeOriginal'), record.get('SubSecTimeOriginal'),
                                                            record.get('OffsetTimeOriginal'))
                                    fallback = datetime.fromtimestamp(mtime / 1e9, timezone.utc).replace(tzinfo=None).isoformat(sep=' ', timespec='microseconds')
                                    save(db, dict(archive_id=identity, stable_id=stable_id(identity, relative),
                                         full_path=path, relative_path=relative, filename=Path(path).name,
                                         file_size=size, mtime_ns=mtime,
                                         width=record.get('ImageWidth') or record.get('ExifImageWidth'),
                                         height=record.get('ImageHeight') or record.get('ExifImageHeight'),
                                         datetime_original=record.get('DateTimeOriginal'), subsecond_original=record.get('SubSecTimeOriginal'),
                                         timezone_offset=record.get('OffsetTimeOriginal'), captured_local=local, captured_utc=utc,
                                         chronological_at=local or fallback, camera_make=record.get('Make'), camera_model=record.get('Model'),
                                         first_catalogued_at=scanned, last_scanned_at=scanned))
                                    counts.catalogued += 1
                                except (OSError, ValueError, sqlite3.Error) as exc:
                                    error(path, exc)
                        finally:
                            db.commit()
                    # Errors count as completed for percentage/ETA, including noncatalogued candidates.
                    counts.processed += len(batch)
                    display.update()
        finally:
            queue.close()
    return counts
