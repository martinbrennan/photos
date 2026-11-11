PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS archives (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    archive_id TEXT NOT NULL REFERENCES archives(id),
    stable_id TEXT NOT NULL UNIQUE,
    full_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    datetime_original TEXT,
    subsecond_original TEXT,
    timezone_offset TEXT,
    captured_local TEXT,
    captured_utc TEXT,
    chronological_at TEXT NOT NULL,
    camera_make TEXT,
    camera_model TEXT,
    first_catalogued_at TEXT NOT NULL,
    last_scanned_at TEXT NOT NULL,
    UNIQUE(archive_id, relative_path)
);
CREATE INDEX IF NOT EXISTS photos_chronological ON photos(chronological_at);
CREATE INDEX IF NOT EXISTS photos_capture_local ON photos(captured_local);
CREATE INDEX IF NOT EXISTS photos_capture_utc ON photos(captured_utc);
CREATE INDEX IF NOT EXISTS photos_archive ON photos(archive_id);
PRAGMA user_version = 1;
