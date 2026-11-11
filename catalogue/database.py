import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript((Path(__file__).resolve().parent.parent / 'schema.sql').read_text())
    return db


def archive(db, root):
    row = db.execute('SELECT id FROM archives WHERE root_path=?', (str(root),)).fetchone()
    if row:
        return row['id']
    identity = str(uuid.uuid4())
    with db:
        db.execute('INSERT INTO archives VALUES (?,?,?)', (identity, str(root), now()))
    return identity


def stable_id(archive_id, relative):
    return hashlib.sha256((archive_id + '\0' + relative).encode('utf-8', 'surrogateescape')).hexdigest()


def save(db, values):
    columns = list(values)
    updates = ','.join(f'{c}=excluded.{c}' for c in columns
                       if c not in ('archive_id', 'relative_path', 'stable_id', 'first_catalogued_at'))
    db.execute(f"INSERT INTO photos ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
               f'ON CONFLICT(archive_id,relative_path) DO UPDATE SET {updates}',
               list(values.values()))


def photo_values(archive_id, root, path, record, stat, scanned=None):
    """Build a catalogue row from metadata already read, without decoding pixels."""
    from .metadata import timestamps
    scanned = scanned or now()
    local, utc = timestamps(record.get('DateTimeOriginal'), record.get('SubSecTimeOriginal'),
                            record.get('OffsetTimeOriginal'))
    fallback = datetime.fromtimestamp(stat.st_mtime_ns / 1e9, timezone.utc).replace(tzinfo=None).isoformat(sep=' ', timespec='microseconds')
    relative = str(Path(path).relative_to(root))
    return dict(archive_id=archive_id, stable_id=stable_id(archive_id, relative),
                full_path=str(path), relative_path=relative, filename=Path(path).name,
                file_size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                width=record.get('ImageWidth') or record.get('ExifImageWidth'),
                height=record.get('ImageHeight') or record.get('ExifImageHeight'),
                datetime_original=record.get('DateTimeOriginal'), subsecond_original=record.get('SubSecTimeOriginal'),
                timezone_offset=record.get('OffsetTimeOriginal'), captured_local=local, captured_utc=utc,
                chronological_at=local or fallback, camera_make=record.get('Make'), camera_model=record.get('Model'),
                first_catalogued_at=scanned, last_scanned_at=scanned)
