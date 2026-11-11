"""Local slideshow server. Catalogue connections are strictly read-only."""
import json
import logging
import random
import sqlite3
import threading
import subprocess
import sys
import time
from contextlib import closing
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False

WEB = Path(__file__).parent / 'web'
SUPPORTED = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'} | ({'.heic', '.heif'} if HEIF else set())
log = logging.getLogger(__name__)


def read_database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db



class PhotoStore:
    def __init__(self, database):
        self.database = Path(database).resolve()
        self.image_lock = threading.Lock()
        self.failed = {}
        self.render_timeout = 25
        # Per-server cache, bounded to eight screen-sized JPEGs, never original pixels.
        self.render = lru_cache(maxsize=8)(self._render)

    def photo(self, photo_id):
        with closing(read_database(self.database)) as db:
            return db.execute('SELECT p.*,a.root_path FROM photos p JOIN archives a ON a.id=p.archive_id '
                              'WHERE p.id=?', (photo_id,)).fetchone()

    def pick(self, previous=None):
        with closing(read_database(self.database)) as db:
            # One short snapshot keeps count and random offset consistent while scans commit.
            db.execute('BEGIN')
            count = db.execute('SELECT COUNT(*) FROM photos').fetchone()[0]
            if not count:
                return None
            for attempt in range(40):
                row = db.execute('SELECT p.*,a.root_path FROM photos p JOIN archives a ON a.id=p.archive_id '
                                 'ORDER BY p.id LIMIT 1 OFFSET ?', (random.randrange(count),)).fetchone()
                if count > 1 and row['id'] == previous and attempt < 30:
                    continue
                if Path(row['filename']).suffix.lower() not in SUPPORTED:
                    continue
                if self.failed.get(row['id'], 0) > time.monotonic():
                    continue
                return {'id': row['id'], 'filename': row['filename'], 'path': row['relative_path'],
                        'date': row['captured_local'], 'camera': ' '.join(filter(None, [row['camera_make'], row['camera_model']])),
                        'count': count, 'url': f"/photo/{row['id']}"}
        return None

    def adjacent(self, photo_id, direction):
        """Find the nearest usable image in the catalogue's chronological order."""
        operator, order = ('>', 'ASC') if direction == 'next' else ('<', 'DESC')
        with closing(read_database(self.database)) as db:
            db.execute('BEGIN')
            current = db.execute('SELECT chronological_at FROM photos WHERE id=?', (photo_id,)).fetchone()
            if current is None:
                raise FileNotFoundError('Photo is not in the catalogue')
            count = db.execute('SELECT COUNT(*) FROM photos').fetchone()[0]
            rows = db.execute(
                'SELECT p.*,a.root_path FROM photos p JOIN archives a ON a.id=p.archive_id '
                f'WHERE (p.chronological_at,p.id) {operator} (?,?) '
                f'ORDER BY p.chronological_at {order},p.id {order}',
                (current['chronological_at'], photo_id))
            for row in rows:
                if Path(row['filename']).suffix.lower() not in SUPPORTED:
                    continue
                if self.failed.get(row['id'], 0) > time.monotonic():
                    continue
                return {'id': row['id'], 'filename': row['filename'], 'path': row['relative_path'],
                        'date': row['captured_local'], 'camera': ' '.join(filter(None, [row['camera_make'], row['camera_model']])),
                        'count': count, 'url': f"/photo/{row['id']}"}
        return None

    def _render(self, path, root, size, mtime):
        # A native decoder can hold the GIL or hang on disk I/O. Keep it out of
        # the HTTP process, with a deadline that also kills/reaps the child.
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'catalogue.render_worker'],
                cwd=Path(__file__).resolve().parent.parent,
                input=json.dumps({'path': path, 'root': root}).encode(),
                capture_output=True, timeout=self.render_timeout)
        except subprocess.TimeoutExpired as exc:
            log.error('Photo rendering timed out after %.1fs: %s', time.monotonic() - started, path)
            raise OSError('Photo rendering timed out') from exc
        if result.returncode or not result.stdout.startswith(b'\xff\xd8'):
            log.warning('Photo rendering failed: %s: %s', path, result.stderr.decode(errors='replace')[:1000])
            raise OSError('Photo could not be decoded')
        log.info('Rendered photo in %.2fs: %s', time.monotonic() - started, path)
        return result.stdout

    def image(self, photo_id):
        row = self.photo(photo_id)
        if row is None:
            raise FileNotFoundError('Photo is not in the catalogue')
        # No USB I/O in the HTTP process; validation and decoding happen in the child.
        if Path(row['filename']).suffix.lower() not in SUPPORTED:
            raise ValueError('Unsupported photo format')
        if not self.image_lock.acquire(timeout=.25):
            raise OSError('Another photograph is being rendered; retry shortly')
        try:
            return self.render(row['full_path'], row['root_path'], row['file_size'], row['mtime_ns'])
        except (OSError, ValueError):
            if len(self.failed) >= 512:
                self.failed.pop(next(iter(self.failed)))
            self.failed[photo_id] = time.monotonic() + 300
            raise
        finally:
            self.image_lock.release()


def make_server(database, host='127.0.0.1', port=8765, interval=10):
    if not 1 <= interval <= 3600:
        raise ValueError('Interval must be between 1 and 3600 seconds')
    store = PhotoStore(database)
    with closing(read_database(database)) as db:
        db.execute('SELECT id FROM photos LIMIT 1').fetchone()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, content, kind):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def json(self, status, value):
            self.reply(status, json.dumps(value).encode(), 'application/json; charset=utf-8')

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                if url.path in ('/', '/style.css', '/app.js', '/zoom.js'):
                    name, kind = {'/': ('index.html', 'text/html; charset=utf-8'),
                                  '/style.css': ('style.css', 'text/css'),
                                  '/zoom.js': ('zoom.js', 'text/javascript'), '/app.js': ('app.js', 'text/javascript')}[url.path]
                    self.reply(200, (WEB / name).read_bytes(), kind)
                elif url.path == '/api/config':
                    self.json(200, {'interval': interval, 'heif': HEIF})
                elif url.path == '/api/random':
                    value = parse_qs(url.query).get('previous', [''])[0]
                    photo = store.pick(int(value) if value.isdigit() else None)
                    if photo:
                        self.json(200, photo)
                    else:
                        self.json(503, {'error': 'No available photos found yet. Check the source disk; newly catalogued photos will appear automatically.'})
                elif url.path == '/api/adjacent':
                    query = parse_qs(url.query)
                    value = query.get('current', [''])[0]
                    direction = query.get('direction', [''])[0]
                    if not value.isdigit() or direction not in ('next', 'previous'):
                        self.json(400, {'error': 'A current photo and next or previous direction are required.'})
                    else:
                        photo = store.adjacent(int(value), direction)
                        self.json(200, photo if photo else {'end': True})
                elif url.path.startswith('/photo/') and url.path[7:].isdigit():
                    self.reply(200, store.image(int(url.path[7:])), 'image/jpeg')
                else:
                    self.json(404, {'error': 'Not found'})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (FileNotFoundError, ValueError):
                self.json(404, {'error': 'Photo unavailable; trying another.'})
            except (OSError, sqlite3.Error, Image.DecompressionBombError):
                log.exception('Slideshow request failed: %s', self.path)
                self.json(503, {'error': 'Could not load this photo. The slideshow will retry.'})

        def log_message(self, message, *args):
            log.debug(message, *args)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.photo_store = store
    return server


def serve(database, host='127.0.0.1', port=8765, interval=10):
    server = make_server(database, host, port, interval)
    print(f'Slideshow: http://{host if host != "0.0.0.0" else "127.0.0.1"}:{server.server_port}', flush=True)
    print('Ctrl+C stops the slideshow server. The scan can continue in its own terminal.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nSlideshow stopped.')
    finally:
        server.server_close()
