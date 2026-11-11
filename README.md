# Local Photo Catalogue

A Python 3.10+ command-line catalogue for an Ubuntu photo archive. Originals are
only read. Scanning does not decode images or generate thumbnails or image-content hashes.
An optional local browser slideshow displays photos already catalogued. Albums
and Google Photos integration are not implemented.

## Install on Ubuntu

```bash
sudo apt update
sudo apt install python3 python3-venv libimage-exiftool-perl
cd /home/martin/Popeye
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
exiftool -ver
```

ExifTool is a separate system executable, not a pip package. It reads headers and
metadata without decoding full-resolution pixels. Batches of 64 files amortize
process startup; there is no process per photograph in normal operation. JPEG,
PNG, TIFF, HEIC/HEIF and WebP are candidates, case-insensitively. Actual support
for a particular format or metadata field depends on the installed ExifTool.
The scanner itself does not need an image decoder. The slideshow uses Pillow
(included in requirements.txt); optional HEIC support uses pillow-heif.

## Usage

Run from the project directory to store the database and log there:

```bash
python photo_catalogue.py scan /media/martin/Photos --mount-point /media/martin/Photos
python photo_catalogue.py stats
python photo_catalogue.py random
python photo_catalogue.py list \
  --from "2026-10-27 14:00:00" \
  --to "2026-10-27 16:00:00"
```

If the archive is a subdirectory of the mounted disk, specify the volume's actual
mount location, for example `scan /media/martin/Photos/Archive --mount-point
/media/martin/Photos`. The explicit mount point must be mounted and contain the
photo root. This is the recommended USB configuration. Without that option,
roots under `/media`, `/mnt` or `/run` require a mounted ancestor other than `/`.
Other paths are permitted for local archives and tests; use `--mount-point` for
USB disks mounted elsewhere. Mount validation verifies the mount location, not
the physical disk UUID: ensure the intended disk is mounted there.

Global options go **before** the command:

```bash
python photo_catalogue.py --db /home/martin/Popeye/archive.sqlite3 \
  --log /home/martin/Popeye/archive.log scan /media/martin/Photos
python photo_catalogue.py --db /home/martin/Popeye/archive.sqlite3 stats
```

Both outputs must be outside the photo root. Database parent directories must
already exist. Keep the database on the NUC, not the USB drive. SQLite creates
WAL/SHM sidecars alongside it. `--batch-size 32` lowers the metadata batch bound
(allowed range 1–256); `--no-progress` suits scheduled/noninteractive runs.
`--exiftool /path/to/exiftool` selects an executable.

Discovery displays directories visited, candidates found, elapsed time and the
current directory. Discovery cannot know the total in advance. Cataloguing then
shows percentage, completed/unchanged/error counts, throughput and estimated time
remaining once Rich has enough timing samples. Throughput includes unchanged
files; metadata-heavy batches and different formats can make ETA fluctuate.

`random` prints one uniformly selected catalogue path without opening it. It uses
a random offset in ID order, tolerates sparse IDs and costs O(N) in the worst
case. It includes all catalogue rows, including temporarily unavailable files.
`list` streams matching timestamps and paths (tab separated), with inclusive
endpoints and no default result limit. Spaces and Unicode in filenames are
supported; paths containing newlines are valid but naturally span output lines.

## Merge a Google Takeout export

Extract the downloaded ZIP/TGZ files first. The importer accepts a source folder,
walks its subdirectories and considers the same image formats as the scanner.
It does not process ZIPs directly or import videos. Source files stay untouched.
The default destination is `/run/media/martin/Popeye`; an explicit destination
root can be supplied as the second positional argument.

Start with a dry-run (replace the example source with your extracted folder):

```bash
cd /home/martin/Popeye
python3 photo_catalogue.py import-takeout \
  "/home/martin/Downloads/Takeout/Google Photos" \
  /run/media/martin/Popeye \
  --mount-point /run/media/martin/Popeye \
  --dry-run --report takeout-preview.csv
```

Read the CSV report, then run the import:

```bash
python3 photo_catalogue.py import-takeout \
  "/home/martin/Downloads/Takeout/Google Photos" \
  /run/media/martin/Popeye \
  --mount-point /run/media/martin/Popeye \
  --report takeout-import.csv
```

Report files must be new and outside both photo trees; existing reports are never
replaced. Omit `--report` for a timestamped CSV in the current directory. Use a new
report name on each run. Global `--db` and `--log` options go before the command;
`--batch-size`, `--exiftool` and `--no-progress` work as for scanning.

The destination volume must be mounted and writable for an actual import. Dry-runs
also work against a read-only destination: they refresh a temporary SQLite snapshot
and leave the original catalogue and photo trees untouched. They write only the
report, log and catalogue writer-lock sidecar. Temporary disk space must accommodate
the catalogue snapshot, file listing and metadata matching index.

### Import steps and matching rule

1. Refresh the local catalogue incrementally, so photos not previously scanned
   can prevent duplicate imports. If this refresh reports file errors, the import
   stops rather than assuming those local photos are absent.
2. Discover Takeout image files using a disk-backed queue, then build an indexed,
   disk-backed lookup of the chosen local archive's EXIF identities.
3. Compare each source photo, copy unmatched files and register each successful
   copy in SQLite. Newly imported photos can prevent subsequent duplicate copies
   within the same Takeout run, even across different Google album folders.

EXIF is used to shortlist candidates by capture date/time, camera make/model and
image width/height. Subseconds are included where available, including precision
beyond six digits. Make/model matching ignores case and repeated whitespace.
When both images have timezone offsets, capture times are compared in UTC.
Images with unknown offsets are compared using local camera times; offsets are
not guessed.

**Matching EXIF alone never causes a duplicate skip.** A candidate must also be
byte-for-byte identical to the source image. This preserves every distinct burst
frame even when several frames have the same whole-second timestamp, dimensions
and camera, or even the same recorded subseconds. The check applies to existing
local photos and photos newly copied or planned during this import. Dry-runs use
the original source bytes for planned destinations, so their decisions agree with
actual imports. A candidate must remain present and unchanged; unreadable or
changed candidates do not justify skipping the source.

Byte comparisons use bounded chunks, check size first and stop at the first
difference. Only EXIF candidates are read this way; the archive is not subjected
to a full hash or pixel-similarity scan. Missing/incompatible EXIF results in a
conservative copy (or reuse of an identical destination filename). A recompressed
Google copy, or a file with changed embedded metadata, can be retained even if it
shows the same scene: differing bytes are deliberately preserved rather than
risk losing a photograph. Only the chosen local archive is searched.

### Destination layout and collisions

For example:

```text
Source:      Google Photos/Holidays 2024/IMG_1234.jpg
Destination: /run/media/martin/Popeye/takeout/Holidays 2024/IMG_1234.jpg
```

Only the **immediate parent folder name** is retained. An image directly in the
chosen source folder uses that folder's own name as its parent. Different source
branches with the same final folder name merge into the same `takeout/` folder.
Existing images are never replaced. Different files with the same destination
filename receive a deterministic `__<source-path-digest>` suffix, with a counter
if needed. When a filename collision is byte-identical, the existing file is
registered/reused instead. Bounded byte comparison also handles filename collisions
and recovery, without scanning the whole archive for content matches. A confirmed duplicate is
not additionally copied into every Google album that contains it.

Unambiguous sidecars named `<image filename>.json` or
`<image filename>.supplemental-metadata.json` are copied beside imported images
(and follow collision renaming). Existing conflicting sidecars are reported and
preserved. JSON contents are not written into EXIF or used as a substitute for
EXIF matching. Truncated/renamed sidecars, stem-only JSON and other standalone
JSON files are not guessed or imported. Keep your original Takeout export if
you need those additional records; Google-provided dates can differ from EXIF.

### Progress, summary and recovery

The terminal shows discovery counts and, during import, percentage/ETA, rate,
current file, copied/planned images, confirmed duplicate skips, already imported files, uncertain
metadata, collision renames, bytes and errors. A final summary includes SQLite
registrations and sidecar copies. Example (illustrative):

```text
Takeout import summary
Directories: 12 | Discovered: 1,000 | Processed: 1,000
Copied: 150 | Would copy: 0 | Confirmed duplicates skipped: 840
Already imported: 10 | Recovered/catalogued: 0 | Uncertain metadata: 25
Collision renames: 2 | JSON sidecars copied: 145
Registered in SQLite: 150
Copied bytes: 912,345,678 | Errors: 0
Report: /home/martin/Popeye/takeout-import.csv
```

The CSV records each candidate's action, source, destination, reason and matched
photo ID where available. The normal error log also records problematic files.
Unreadable/corrupt source images are reported and skipped; disk-full, read-only
or I/O failures stop the run. A partially completed summary is printed on Ctrl+C.
Exit status is 0 on success, 1 for per-file errors, 2 for a fatal failure and 130
for interruption.

New image bytes are copied to a temporary file in the destination folder, flushed
and atomically published without replacing an existing file. Ubuntu's
`renameat2(RENAME_NOREPLACE)` is used, with a hard-link fallback on filesystems
that support it. An error is reported rather than using an unsafe replacement
when neither operation is supported. A lazy-created `takeout_imports` table
journals each source root/path, size/mtime and destination before copying. The
photo row and completed journal status commit together. Rerunning the same
command recognises completed copies even without EXIF; a crash between publication
and registration is recovered by comparing bytes before registering the file.
Only a job's own journalled image temporary file is cleaned during recovery.

Keep the extracted source folder stable while running. Changing its root/path or
file size/mtime changes its import fingerprint. The new `takeout_imports` table
contains source provenance; it is created only when the import facility is used.
Each registered photo belongs to the existing destination archive, uses the normal
stable ID and becomes available to list/stats/random/slideshow immediately.

Run one scanner/importer per database at a time. Updated CLI writers share a
`*.writer.lock` file and report an error if another writer holds it. An older
scanner already running before this update cannot participate in the new lock;
let it finish before importing. The slideshow can remain running throughout.
As with scanning, size/mtime checks assume a stable, trusted filesystem. Changes
that deliberately preserve both size and modification time cannot be detected.

## Browser slideshow (works during a scan)

Keep the scan running in its current terminal. Open a **second terminal**:

```bash
cd /home/martin/Popeye
# Activate .venv if you installed dependencies in a virtual environment.
python3 photo_catalogue.py slideshow --interval 10
```

Open <http://127.0.0.1:8765> on the NUC. Change the interval in the page, pause/play,
advance manually, or use fullscreen. Back retraces successfully displayed images
in this tab's session. Previous Image and Next Image navigate chronologically
(using the catalogue's modification-time fallback for unknown capture dates),
stopping at either end; matching dates are ordered by photo ID. Next Random and
automatic playback choose random images. Space pauses, Left/Right navigate
chronologically, B goes back, R chooses randomly, and F opens fullscreen when
focus is outside a control. The interval is measured after
the photo finishes loading; the browser remembers changes on this device. A
remembered interval takes precedence over the initial `--interval` value.

In either the normal or fullscreen image view, scroll up over the image to
zoom in about the cursor, and scroll down to zoom out. Four wheel notches double
the magnification. Zoom ranges from fit-to-window to two physical screen pixels
per displayed preview pixel (four screen pixels in area); fit takes precedence
if a small image already needs greater enlargement. Previews remain limited to
2560 pixels per side. Changing image or resizing the view resets zoom to fit.

The viewer reads the catalogue using SQLite `mode=ro` and `query_only`. It never
runs schema setup or writes catalogue records. WAL allows the ongoing scan to
commit while the viewer uses brief read snapshots. Newly committed photos become
eligible automatically; no restart or completed scan is necessary. The number
shown is the catalogue count at the last selection, not a separate progress feed.
There is little to show until discovery finishes and the scanner starts committing
photo rows. Both processes share the disk and CPU, so viewing may slightly slow
the scan.

Random choices sample catalogue rows uniformly, skip recently failed files and try
to avoid an immediate repeat. Selection is with replacement; there is no persistent
shuffle history. After 40 unsuccessful attempts the page reports that no available
photos were found and retries after five seconds while playing. Missing or corrupt
images also trigger a retry; failed files are skipped for five minutes (up to 512
recent failures held in memory). If most entries point to a disconnected disk, reconnect it.
No catalogue entries are removed or changed by the viewer.

JPEG, PNG, WebP and TIFF are displayed as transient, screen-sized JPEGs (at most
2560 pixels per side), with EXIF orientation applied. Only the viewer decodes
pixels. JPEG draft decoding reduces work when possible; TIFF/PNG and some other
formats can still require decoding the full source image. Conversions run in disposable subprocesses, with a 25-second timeout and a
90-megapixel image limit. One conversion runs at a time; concurrent image requests
receive a retry response instead of waiting indefinitely. The HTTP server stays
responsive during decoding, with at most eight rendered JPEGs cached in RAM.
Next can cancel/skip an in-progress browser request; loading has a 35-second
browser deadline. Photo failures and render timings are logged in photo_catalogue.log. No thumbnails or cache files are written to disk,
and original files remain untouched. Very large or damaged files may be rejected
by Pillow's image size protections.

HEIC/HEIF viewing is optional:

```bash
python3 -m pip install pillow-heif
```

Restart the slideshow server after installing it. Without that package, HEIC/HEIF
entries remain catalogued but are skipped by the slideshow.

To view from another device on your trusted home network, explicitly bind the
server to the NUC's network interfaces:

```bash
python3 photo_catalogue.py slideshow --host 0.0.0.0 --port 8765 --interval 15
```

On that device visit `http://<NUC-IP>:8765`. The default is NUC-only. This simple
home viewer has no login or HTTPS; do not expose port 8765 to the Internet. Stop
the viewer with Ctrl+C in its own terminal; the scanner continues independently.
Custom database paths use the existing global option:

```bash
python3 photo_catalogue.py --db /path/to/archive.sqlite3 slideshow
```

## Times and identity

`schema.sql` is the SQLite schema. Each archive has a persistent UUID associated
with its canonical absolute root path. A photo's stable identifier is SHA-256 of
that UUID plus its relative path; no image bytes are hashed. Duplicate filenames
in different directories are distinct. The numeric photo ID and initial
cataloguing time survive updates at the same path. Moving a file or changing the
archive root currently creates a new identity; always use the same mount/root
path. Content deduplication and root relocation are future work.

EXIF original date/time, subseconds and offset are stored as supplied. Normalized
local capture timestamps use six fractional digits (longer EXIF subseconds are
preserved separately). UTC capture timestamps are populated only when a valid
offset exists; the scanner does not guess a timezone. Invalid or absent capture
times use filesystem modification time in UTC for the `chronological_at` index.
Consequently, the default chronological timeline mixes camera wall times with UTC
mtime fallbacks. It is useful for browsing, but not an absolute global timeline.

Choose the time basis explicitly when matching across timezones:

```bash
python photo_catalogue.py list --time-basis local \
  --from "2026-10-27 14:00:00" --to "2026-10-27 16:00:00"
python photo_catalogue.py list --time-basis utc \
  --from "2026-10-27T13:00:00+00:00" --to "2026-10-27T15:00:00+00:00"
```

`local` excludes photos without valid capture times; `utc` excludes photos
without offset-aware capture times. Offset-bearing query bounds are converted to
UTC, so use naive bounds for `local`. All normalized timestamps are indexed text
with fixed microsecond precision. Modification time is stored as integer
nanoseconds. Operational timestamps are UTC with an explicit offset.

The photo ID is a suitable foreign key for future albums, thumbnails, hashes and
remote IDs, which can live in separate tables without changing archive identity.
The schema has a version marker; future incompatible migrations must be explicit.

## Performance and reliability

Discovery stores candidates and its directory queue in a temporary on-disk SQLite
database. It does not retain the full archive listing in memory. Ensure the
system temporary directory has space proportional to the number and length of
paths. Metadata extraction holds at most one configured batch in Python memory.
The working database uses WAL and transactional batch writes.

Each scan compares file size and nanosecond modification time. Unchanged photos
skip metadata extraction and update their last-scanned timestamp. This comparison
cannot detect content changed while deliberately preserving size and mtime.
Changed files are checked again after metadata extraction and retried next run
if they changed during processing. Failed extraction is logged and retried on
the next scan; an earlier good row is retained. Missing EXIF is acceptable.

Symlinks found during traversal and a symlink supplied as the root are skipped or
rejected. Treat the archive as a stable, trusted filesystem while scanning; this
is not a hardened reader for directories being maliciously replaced concurrently.
No missing rows are deleted. The root/mount is checked initially and between
metadata batches. Read failures are logged and do not stop other files; loss of
the root/mount stops the scan while retaining committed work.

Ctrl+C saves completed database work and returns status 130. Restart with the same
command: completed photos are skipped. Discovery is rebuilt after interruption;
its temporary queue is not a durable job list. Each ExifTool batch has a 120-second
timeout; failed batches retry individually to isolate problematic files. Errors
are written to `photo_catalogue.log`; ExifTool warnings are logged as warnings.
Exit codes: 0 success, 1 completed with file errors (or empty random selection),
2 configuration/operational failure, 130 interrupted. Run one scan at a time
against a database, and back it up with SQLite's backup API when writers are active.

## Example statistics (illustrative)

```text
Archives: 1
Photographs: 42,817
Source bytes: 843,291,680,102
With capture time: 41,905
Earliest: 2003-05-14 09:21:30.000000
Latest: 2026-10-27 15:58:02.123000
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use temporary archives and a controlled metadata reader, so they don't need
ExifTool or access to the real USB disk. They cover database updates, same-name
identity, incremental skips, missing metadata, corrupt-file retries, symlink
exclusion, restart after interruption, mount validation, time normalization,
ExifTool argument handling and all read-only CLI commands. A real archive scan
requires the system ExifTool dependency installed above.

Slideshow tests also cover HTTP endpoints, image rendering, source preservation,
missing files, path boundaries, read-only enforcement and reads concurrent with a
write transaction. HTTP tests need permission to bind a local loopback socket.
