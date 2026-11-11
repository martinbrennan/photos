import io
import json
import sqlite3
import tempfile
import threading
import unittest
import subprocess
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from PIL import Image

from catalogue.database import connect
from catalogue.scanner import scan
from catalogue.slideshow import PhotoStore, make_server, read_database


class Reader:
    def read(self, paths):
        return {path: {} for path in paths}


class SlideshowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'photos'
        self.root.mkdir()
        self.photo = self.root / 'a space é.jpg'
        Image.new('RGB', (80, 40), 'red').save(self.photo)
        self.database = self.base / 'catalogue.sqlite3'
        self.writer = connect(self.database)
        scan(self.writer, self.root, Reader(), show_progress=False)
        self.store = PhotoStore(self.database)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def test_readonly_and_live_catalogue(self):
        db = read_database(self.database)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM photos')
        finally:
            db.close()
        self.assertEqual(self.store.pick()['count'], 1)
        Image.new('RGB', (10, 10)).save(self.root / 'second.png')
        scan(self.writer, self.root, Reader(), show_progress=False)
        first = self.store.pick()
        second = self.store.pick(first['id'])
        self.assertEqual(second['count'], 2)
        self.assertNotEqual(first['id'], second['id'])
        # A writer can hold a transaction while the viewer reads committed rows.
        self.writer.execute('UPDATE photos SET camera_make=?', ('pending',))
        self.assertEqual(self.store.pick()['count'], 2)
        self.writer.rollback()

    def test_render_and_original_unchanged(self):
        before = self.photo.read_bytes()
        photo = self.store.pick()
        rendered = self.store.image(photo['id'])
        with Image.open(io.BytesIO(rendered)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (80, 40))
        self.assertEqual(self.photo.read_bytes(), before)
        self.assertEqual(self.store.image(photo['id']), rendered)
        self.assertEqual(self.store.render.cache_info().hits, 1)

    def test_unavailable_and_outside_archive(self):
        photo_id = self.store.pick()['id']
        self.photo.unlink()
        with self.assertRaises(OSError):
            self.store.image(photo_id)
        self.assertIsNone(self.store.pick())
        outside = self.base / 'private.jpg'
        Image.new('RGB', (10, 10)).save(outside)
        self.writer.execute('UPDATE photos SET full_path=?', (str(outside),))
        self.writer.commit()
        with self.assertRaises(OSError):
            self.store.image(photo_id)

    def test_timeout_releases_renderer_and_quarantines_bad_photo(self):
        photo_id = self.store.pick()['id']
        with patch('catalogue.slideshow.subprocess.run', side_effect=subprocess.TimeoutExpired('decoder', 25)):
            with self.assertRaises(OSError):
                self.store.image(photo_id)
        self.assertIsNone(self.store.pick())
        # The worker slot is released and another photograph can render normally.
        Image.new('RGB', (10, 10)).save(self.root / 'healthy.jpg')
        scan(self.writer, self.root, Reader(), show_progress=False)
        healthy = self.store.pick()
        self.assertNotEqual(healthy['id'], photo_id)
        self.assertTrue(self.store.image(healthy['id']).startswith(b'\xff\xd8'))

    def test_page_responds_while_decoder_is_stuck(self):
        server = make_server(self.database, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        entered, release = threading.Event(), threading.Event()
        original = server.photo_store._render

        def stalled(*args):
            entered.set()
            release.wait(5)
            return original(*args)

        server.photo_store.render = stalled
        base = f'http://127.0.0.1:{server.server_port}'
        photo_id = self.store.pick()['id']
        image_thread = threading.Thread(target=lambda: urlopen(base + f'/photo/{photo_id}', timeout=10).close())
        try:
            image_thread.start()
            self.assertTrue(entered.wait(3))
            with urlopen(base + '/', timeout=2) as response:
                self.assertIn(b'POPEYE', response.read())
            # More image requests fail promptly rather than accumulating behind a lock.
            with self.assertRaises(HTTPError) as error:
                urlopen(base + f'/photo/{photo_id}', timeout=2)
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
        finally:
            release.set()
            image_thread.join(10)
            server.shutdown()
            server.server_close()
            thread.join()

    def test_http_endpoints(self):
        server = make_server(self.database, port=0, interval=7)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base + '/') as response:
                self.assertIn(b'POPEYE', response.read())
            with urlopen(base + '/api/config') as response:
                self.assertEqual(json.load(response)['interval'], 7)
            with urlopen(base + '/api/random') as response:
                photo = json.load(response)
            with urlopen(base + photo['url']) as response:
                self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
                self.assertTrue(response.read().startswith(b'\xff\xd8'))
            for path in ['/photo/9999999', '/photo/../../README.md', '/schema.sql']:
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + path)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            self.photo.unlink()
            server.photo_store.render.cache_clear()
            with self.assertRaises(HTTPError) as failed_image:
                urlopen(base + photo['url'])
            failed_image.exception.close()
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/api/random')
            self.assertEqual(error.exception.code, 503)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
