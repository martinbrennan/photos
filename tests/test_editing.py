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
        self.assertEqual(recipe({}), {'version': 1, 'gamma': 1.0, 'pop': 0.0, 'drama': {'strength': 0.0, 'saturation': 0.0}})
        for value in [{'gamma': float('nan')}, {'gamma': 0}, {'gamma': True}, {'version': 2}, {'pop': -1}, {'pop': 101}, {'pop': True}, {'pop': float('nan')}, {'pop': {}}, {'drama': 1}, {'drama': {'strength': 101}},
                      {'drama': {'saturation': -101}}, {'drama': {'strength': True}},
                      {'drama': {'saturation': float('nan')}}, {'drama': {'style': 'dark'}}]:
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

    def test_drama_noop_composition_saturation_and_original(self):
        import pyvips
        from catalogue.editing import drama, canonical
        xy = pyvips.Image.xyz(800, 500)
        grey = 110 + 25 * (xy[0] * 57.2958 / 10).sin()
        image = grey.bandjoin([grey * .8, grey * .6]).cast('uchar').copy(interpretation='srgb')
        image.jpegsave(str(self.path))
        before = self.path.read_bytes()
        for pop in [0, 40]:
            old = process(self.path, {'gamma': .85, 'pop': pop}, 1440)
            for saturation in [-100, 0, 100]:
                self.assertEqual(old, process(self.path, {'gamma': .85, 'pop': pop,
                    'drama': {'strength': 0, 'saturation': saturation}}, 1440))
        previous = 0
        for strength in [50, 100]:
            output = drama(image, strength)
            difference = (output - image).abs().avg()
            self.assertGreater(difference, previous)
            previous = difference
            self.assertLess(abs(output.colourspace('lab')[0].avg() - image.colourspace('lab')[0].avg()), 6)
        neutral = drama(image, 100, -100)
        self.assertLess((neutral[0] - neutral[1]).abs().max(), 2)
        chroma = lambda im: (im.colourspace('lab')[1] ** 2 + im.colourspace('lab')[2] ** 2).avg()
        self.assertGreater(chroma(drama(image, 50, 100)), chroma(drama(image, 50, 0)))
        self.assertLess(chroma(drama(image, 50, -100)), chroma(drama(image, 50, 0)))
        self.assertLess((drama(image, 50) - drama(image.resize(2), 50).resize(.5)).abs().avg(), 2)
        combined = process(self.path, {'gamma': .85, 'pop': 30,
                           'drama': {'strength': 50, 'saturation': -20}}, 1440)
        self.assertNotEqual(combined, process(self.path, {'gamma': .85, 'pop': 30}, 1440))
        self.assertNotEqual(canonical({'drama': {'strength': 50}}), canonical({}))
        self.assertEqual(before, self.path.read_bytes())

    def test_drama_sky_noise_and_extreme_boundaries(self):
        import pyvips
        from catalogue.editing import drama
        xy = pyvips.Image.xyz(800, 400)
        gradient = 100 + xy[0] / 8
        sky = gradient.bandjoin([gradient + 20, gradient + 40]).cast('uchar').copy(interpretation='srgb')
        result = drama(sky, 100)
        # Smooth gradients should remain smooth, without major banding steps.
        step = result.crop(1, 0, 799, 400) - result.crop(0, 0, 799, 400)
        self.assertLess(step.abs().max(), 4)
        boundary = (xy[0] < 400).ifthenelse(0, 255)
        image = boundary.bandjoin([boundary, boundary]).cast('uchar').copy(interpretation='srgb')
        self.assertLess((drama(image, 100) - image).abs().max(), 2)
        noise = pyvips.Image.gaussnoise(800, 400, mean=25, sigma=2, seed=42).cast('uchar')
        image = noise.bandjoin([noise, noise]).copy(interpretation='srgb')
        result = drama(image, 100)
        self.assertLess(result[0].deviate(), 3 * image[0].deviate())
        for level in [0, 255]:
            flat = pyvips.Image.black(800, 400).new_from_image([level] * 3).cast('uchar').copy(interpretation='srgb')
            self.assertLess((drama(flat, 100) - flat).abs().max(), 2)

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
                              data=b'{"version":1,"gamma":0.8,"pop":40,"drama":{"strength":50,"saturation":-20}}',
                              headers={'Content-Type': 'application/json'})
            with urlopen(request) as response:
                self.assertEqual(json.load(response)['drama'], {'strength': 50, 'saturation': -20})
            with urlopen(base + f'/api/edits/{photo_id}') as response:
                self.assertEqual(json.load(response)['drama'], {'strength': 50, 'saturation': -20})
            try:
                import pyvips
            except (ImportError, OSError):
                return
            for endpoint in [f'/photo/{photo_id}', f'/api/render/{photo_id}?gamma=1.2&pop=40&drama=50&drama_saturation=-20&edge=1440']:
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
