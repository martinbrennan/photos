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
        self.assertEqual(recipe({}), {'version': 1, 'gamma': 1.0, 'pop': 0.0})
        for value in [{'gamma': float('nan')}, {'gamma': 0}, {'gamma': True}, {'version': 2}, {'pop': -1}, {'pop': 101}, {'pop': True}, {'pop': float('nan')}, {'pop': {}}]:
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

    def test_pop_noop_cache_and_texture(self):
        import pyvips
        from catalogue.editing import local_contrast
        xy = pyvips.Image.xyz(1600, 1000)
        light = 50 + 3 * (xy[0] * 57.2958 / 4).sin() + 8 * (xy[1] * 57.2958 / 24).sin()
        image = light.bandjoin([light, light]).cast('uchar').copy(interpretation='srgb')
        image.jpegsave(str(self.path))
        before = self.path.read_bytes()
        for gamma in [.8, 1, 1.5]:
            self.assertEqual(process(self.path, {'gamma': gamma}, 1440),
                             process(self.path, {'gamma': gamma, 'pop': 0}, 1440))
        cache = Path(self.temp.name) / 'cache'; cache.mkdir()
        expected = process(self.path, {'gamma': .8, 'pop': 40}, 1440)
        self.assertEqual(expected, process(self.path, {'gamma': .8, 'pop': 40}, 1440, cache))
        self.assertEqual(len(list(cache.glob('*.v'))), 1)
        # A warm cache must avoid decoding the original again.
        from unittest.mock import patch
        with patch.object(pyvips.Image, 'thumbnail', side_effect=AssertionError('source decoded')):
            self.assertEqual(expected, process(self.path, {'gamma': .8, 'pop': 40}, 1440, cache))
        baseline = image.colourspace('lab')[0]
        for strength in [40, 100]:
            enhanced = local_contrast(image, strength).colourspace('lab')[0]
            self.assertGreater(enhanced.deviate(), baseline.deviate())
            self.assertLess(abs(enhanced.avg() - baseline.avg()), .5)
            self.assertLess((enhanced - baseline).abs().max(), 25)
        self.assertEqual(self.path.read_bytes(), before)

    def test_pop_smooth_noise_boundaries_and_resolution(self):
        import pyvips
        from catalogue.editing import local_contrast
        xy = pyvips.Image.xyz(1600, 1000)
        # Smooth sky, dark noise, skin colour and a sharp architectural boundary.
        for rgb in [(90, 160, 220), (15, 18, 20), (185, 135, 110)]:
            flat = pyvips.Image.black(1600, 1000).new_from_image(rgb)
            delta = local_contrast(flat, 100) - flat
            self.assertLess(delta.abs().max(), 1)
        boundary = (xy[0] < 800).ifthenelse(30, 220)
        rgb = boundary.bandjoin([boundary, boundary]).cast('uchar').copy(interpretation='srgb')
        delta = local_contrast(rgb, 100).colourspace('lab')[0] - rgb.colourspace('lab')[0]
        self.assertLess(delta.abs().max(), 25)
        texture = (100 + 15 * (xy[0] * 57.2958 / 12).sin() + 20 * (xy[1] * 57.2958 / 60).sin())
        rgb = texture.bandjoin([texture, texture]).cast('uchar').copy(interpretation='srgb')
        low = local_contrast(rgb, 40)
        high = local_contrast(rgb.resize(2), 40).resize(.5)
        self.assertLess((low - high).abs().avg(), 1.5)

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
                              data=b'{"version":1,"gamma":0.8,"pop":40}',
                              headers={'Content-Type': 'application/json'})
            with urlopen(request) as response:
                self.assertEqual(json.load(response)['pop'], 40)
            with urlopen(base + f'/api/edits/{photo_id}') as response:
                self.assertEqual(json.load(response)['pop'], 40)
            try:
                import pyvips
            except (ImportError, OSError):
                return
            for endpoint in [f'/photo/{photo_id}', f'/api/render/{photo_id}?gamma=1.2&pop=40&edge=1440']:
                with urlopen(base + endpoint) as response:
                    self.assertTrue(response.read().startswith(b'\xff\xd8'))
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_saved_recipe_reopen_reset_and_cache(self):
        photo_id = self.store.pick()['id']
        self.assertEqual(self.store.edits(photo_id)['gamma'], 1)
        self.store.save_edits(photo_id, {'gamma': .8, 'pop': 40})
        self.assertEqual(self.store.edits(photo_id)['pop'], 40)
        from catalogue.slideshow import PhotoStore
        self.assertEqual(PhotoStore(self.database).edits(photo_id)['gamma'], .8)
        self.store.save_edits(photo_id, {'gamma': 1})
        self.assertEqual(self.store.edits(photo_id)['gamma'], 1)
        self.assertEqual(self.writer.execute('SELECT revision FROM photo_edits').fetchone()[0], 2)

del SlideshowTests
