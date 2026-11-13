import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageCms
from catalogue.editing import recipe, process
from test_slideshow import SlideshowTests


class RecipeTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(recipe({}), {'version': 1, 'gamma': 1.0})
        for value in [{'gamma': float('nan')}, {'gamma': 0}, {'gamma': True}, {'version': 2}, {'pop': 1}]:
            with self.assertRaises(ValueError):
                recipe(value)


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        try:
            import pyvips
        except (ImportError, OSError):
            self.skipTest('Install pyvips/libvips to exercise processing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'source.jpg'

    def test_gamma_convention_orientation_colour_and_original(self):
        exif = Image.Exif(); exif[274] = 6
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB', (80, 40), (128, 128, 128)).save(self.path, exif=exif, icc_profile=profile)
        before = self.path.read_bytes()
        for edge in [0, 1440]:
            for gamma in [.5, 1, 2]:
                with Image.open(io.BytesIO(process(self.path, {'gamma': gamma}, edge))) as output:
                    self.assertEqual(output.size, (40, 80))
                    self.assertAlmostEqual(output.getpixel((20, 20))[0], 255 * (128 / 255) ** gamma, delta=3)
        self.assertEqual(self.path.read_bytes(), before)

    def test_large_jpeg_preview_and_full_resolution(self):
        Image.new('RGB', (10000, 5000), (100, 140, 180)).save(self.path)
        before = hashlib.sha256(self.path.read_bytes()).digest()
        for edge, expected in [(1440, (1440, 720)), (0, (10000, 5000))]:
            with Image.open(io.BytesIO(process(self.path, {'gamma': .8}, edge))) as output:
                self.assertEqual(output.size, expected)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)


class StorageTests(unittest.TestCase):
    setUp = SlideshowTests.setUp
    tearDown = SlideshowTests.tearDown
    def test_http_save_and_render(self):
        import threading
        from urllib.request import Request, urlopen
        from catalogue.slideshow import make_server
        server = make_server(self.database, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            photo_id = self.store.pick()['id']
            base = f'http://127.0.0.1:{server.server_port}'
            request = Request(base + f'/api/edits/{photo_id}',
                              data=b'{"version":1,"gamma":0.8}',
                              headers={'Content-Type': 'application/json'})
            with urlopen(request) as response:
                self.assertEqual(json.load(response)['gamma'], .8)
            with urlopen(base + f'/api/edits/{photo_id}') as response:
                self.assertEqual(json.load(response)['gamma'], .8)
            try:
                import pyvips
            except (ImportError, OSError):
                return
            for endpoint in [f'/photo/{photo_id}', f'/api/render/{photo_id}?gamma=1.2&edge=1440']:
                with urlopen(base + endpoint) as response:
                    self.assertTrue(response.read().startswith(b'\xff\xd8'))
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_saved_recipe_reopen_reset_and_cache(self):
        photo_id = self.store.pick()['id']
        self.assertEqual(self.store.edits(photo_id)['gamma'], 1)
        self.store.save_edits(photo_id, {'gamma': .8})
        from catalogue.slideshow import PhotoStore
        self.assertEqual(PhotoStore(self.database).edits(photo_id)['gamma'], .8)
        self.store.save_edits(photo_id, {'gamma': 1})
        self.assertEqual(self.store.edits(photo_id)['gamma'], 1)
        self.assertEqual(self.writer.execute('SELECT revision FROM photo_edits').fetchone()[0], 2)

del SlideshowTests
