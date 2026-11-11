import contextlib
import csv
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalogue.database import archive, connect
from catalogue.importer import copy_atomic, import_takeout, publish, signature
from catalogue.locking import writer_lock
from catalogue.scanner import scan
from photo_catalogue import main


def metadata(time='2026:10:27 14:25:00', **overrides):
    value = dict(ImageWidth=9000, ImageHeight=6000, DateTimeOriginal=time,
                 SubSecTimeOriginal='123456789', OffsetTimeOriginal='+01:00', Make='Nikon', Model='D850')
    value.update(overrides)
    return value


class Reader:
    """Small test fixtures map bytes to metadata; production never reads pixels."""
    def __init__(self):
        self.records = {}

    def read(self, paths):
        return {path: self.records.get(Path(path).read_bytes(), {}) for path in paths}


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'local'
        self.source = self.base / 'Google Photos'
        self.root.mkdir()
        self.source.mkdir()
        self.db_path = self.base / 'catalogue.sqlite3'
        self.db = connect(self.db_path)
        self.reader = Reader()
        self.report_counter = 0

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def image(self, tree, relative, content, record=None):
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.reader.records[content] = record or {}
        return path

    def catalogue(self):
        scan(self.db, self.root, self.reader, show_progress=False)

    def run_import(self, dry_run=False, refresh=False):
        self.report_counter += 1
        self.report_path = self.base / f'report-{self.report_counter}.csv'
        with contextlib.redirect_stdout(io.StringIO()):
            result = import_takeout(self.db, self.source, self.root, self.reader, self.report_path,
                                    dry_run=dry_run, show_progress=False, refresh=refresh, batch_size=2)
        return result

    def test_exif_duplicate_and_uncertain_copy_with_database(self):
        original = self.image(self.root, 'old.jpg', b'original', metadata())
        self.catalogue()
        self.image(self.source, 'Album/renamed.JPG', b'original', metadata())
        source = self.image(self.source, 'Vacances été/space é.png', b'no metadata')
        result = self.run_import()
        destination = self.root / 'takeout/Vacances été/space é.png'
        self.assertEqual((result.skipped, result.catalogued, result.uncertain), (1, 1, 1))
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(original.read_bytes(), b'original')
        row = self.db.execute('SELECT * FROM photos WHERE full_path=?', (str(destination),)).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row['captured_local'])
        self.assertIsNotNone(row['chronological_at'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 2)

    def test_differences_in_subseconds_and_dimensions_are_retained(self):
        self.image(self.root, 'local.jpg', b'original', metadata())
        self.catalogue()
        self.image(self.source, 'Album/burst.jpg', b'burst', metadata(SubSecTimeOriginal='123456780'))
        self.image(self.source, 'Album/resized.jpg', b'resized', metadata(ImageWidth=4500, ImageHeight=3000))
        result = self.run_import()
        self.assertEqual((result.catalogued, result.skipped), (2, 0))

    def test_utc_equivalent_exif_with_different_bytes_is_retained(self):
        self.image(self.root, 'local.jpg', b'original', metadata())
        self.catalogue()
        self.image(self.source, 'Album/utc.jpg', b'utc', metadata('2026:10:27 13:25:00', OffsetTimeOriginal='+00:00'))
        self.image(self.source, 'Album/naive.jpg', b'naive', metadata(OffsetTimeOriginal=None))
        result = self.run_import()
        self.assertEqual((result.catalogued, result.skipped), (2, 0))

    def test_burst_without_subseconds_keeps_all_distinct_frames(self):
        same_second = metadata(SubSecTimeOriginal=None)
        self.image(self.root, 'first-local.jpg', b'frame-0', same_second)
        self.catalogue()
        for number in range(1, 4):
            self.image(self.source, f'Burst/frame-{number}.jpg', f'frame-{number}'.encode(), same_second)
        self.image(self.source, 'Other/renamed-duplicate.jpg', b'frame-2', same_second)
        dry = self.run_import(dry_run=True)
        self.assertEqual((dry.planned, dry.skipped), (3, 1))
        result = self.run_import()
        self.assertEqual((result.catalogued, result.skipped), (3, 1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 4)
        self.assertEqual({p.read_bytes() for p in (self.root / 'takeout').rglob('*.jpg')},
                         {b'frame-1', b'frame-2', b'frame-3'})

    def test_distinct_frames_with_identical_subseconds_are_also_retained(self):
        shared = metadata()
        self.image(self.source, 'Burst/one.jpg', b'frame-one', shared)
        self.image(self.source, 'Burst/two.jpg', b'frame-two', shared)
        self.assertEqual(self.run_import().catalogued, 2)

    def test_recompressed_copy_with_matching_exif_is_retained(self):
        self.image(self.root, 'local.jpg', b'original', metadata())
        self.catalogue()
        self.image(self.source, 'Album/recompressed.jpg', b'google recompressed', metadata())
        result = self.run_import()
        self.assertEqual((result.catalogued, result.skipped), (1, 0))

    def test_filename_collisions_never_overwrite(self):
        existing = self.image(self.root, 'takeout/Trips/photo.jpg', b'original')
        self.image(self.source, 'one/Trips/photo.jpg', b'one')
        self.image(self.source, 'two/Trips/photo.jpg', b'two')
        result = self.run_import()
        files = list((self.root / 'takeout/Trips').glob('*.jpg'))
        self.assertEqual((result.catalogued, result.renamed, result.errors), (2, 2, 0))
        self.assertEqual({p.read_bytes() for p in files}, {b'original', b'one', b'two'})
        self.assertEqual(existing.read_bytes(), b'original')

    def test_rerun_without_exif_is_idempotent(self):
        self.image(self.source, 'Album/photo.jpg', b'no metadata')
        self.assertEqual(self.run_import().catalogued, 1)
        result = self.run_import(refresh=True)
        self.assertEqual((result.catalogued, result.resumed), (0, 1))
        self.assertEqual(len(list((self.root / 'takeout/Album').glob('*.jpg'))), 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 1)

    def test_crash_after_publish_is_recovered(self):
        self.image(self.source, 'Album/photo.jpg', b'copy me', metadata())
        with patch('catalogue.importer.register_copy', side_effect=sqlite3.OperationalError('simulated DB failure')):
            result = self.run_import()
        self.assertEqual(result.errors, 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 0)
        self.assertEqual((self.root / 'takeout/Album/photo.jpg').read_bytes(), b'copy me')
        result = self.run_import()
        self.assertEqual((result.catalogued, result.recovered, result.errors), (0, 1, 0))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT status FROM takeout_imports').fetchone()[0], 'complete')

    def test_interrupt_preserves_completed_copies_and_restarts(self):
        self.image(self.source, 'Album/one.jpg', b'one')
        self.image(self.source, 'Album/two.jpg', b'two')
        original = copy_atomic
        calls = 0

        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return original(*args, **kwargs)

        with patch('catalogue.importer.copy_atomic', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_import()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 1)
        result = self.run_import()
        self.assertEqual((result.catalogued, result.resumed), (1, 1))

    def test_dry_run_leaves_archive_and_catalogue_untouched(self):
        self.image(self.root, 'not yet catalogued.jpg', b'local', metadata())
        self.image(self.source, 'Album/duplicate.jpg', b'local', metadata())
        self.image(self.source, 'Album/new.jpg', b'new', metadata('2025:01:01 00:00:00'))
        self.image(self.source, 'Other/same-new.jpg', b'new', metadata('2025:01:01 00:00:00'))
        result = self.run_import(dry_run=True, refresh=True)
        self.assertEqual((result.planned, result.skipped), (1, 2))
        self.assertFalse((self.root / 'takeout').exists())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 0)
        self.assertIsNone(self.db.execute("SELECT name FROM sqlite_master WHERE name='takeout_imports'").fetchone())
        with self.report_path.open() as report:
            rows = list(csv.DictReader(report))
        self.assertEqual(sum(r['action'] == 'would-copy' for r in rows), 1)

    def test_refresh_catalogues_existing_disk_before_matching(self):
        self.image(self.root, 'not yet catalogued.jpg', b'local', metadata())
        self.image(self.source, 'Album/duplicate.jpg', b'local', metadata())
        result = self.run_import(refresh=True)
        self.assertEqual((result.catalogued, result.skipped), (0, 1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 1)

    def test_missing_catalogue_file_does_not_cause_false_skip(self):
        local = self.image(self.root, 'gone.jpg', b'local', metadata())
        self.catalogue()
        local.unlink()
        self.image(self.source, 'Album/copy.jpg', b'google', metadata())
        result = self.run_import()
        self.assertEqual((result.catalogued, result.skipped), (1, 0))

    def test_corrupt_metadata_and_symlinks_are_not_copied(self):
        self.image(self.source, 'Album/bad.jpg', b'bad', {'Error': 'Invalid image'})
        good = self.image(self.source, 'Album/good.jpg', b'good')
        (self.source / 'linked.jpg').symlink_to(good)
        (self.source / 'loop').symlink_to(self.source, target_is_directory=True)
        result = self.run_import()
        self.assertEqual((result.discovered, result.catalogued, result.errors), (2, 1, 1))

    def test_sidecars_preserved_and_retried_after_partial_import(self):
        image = self.image(self.source, 'Album/photo.jpg', b'photo')
        sidecar = image.with_name(image.name + '.supplemental-metadata.json')
        sidecar.write_text('{"photoTakenTime":{"timestamp":"123"}}')
        result = self.run_import()
        target = self.root / 'takeout/Album/photo.jpg.supplemental-metadata.json'
        self.assertEqual(result.sidecars, 1)
        self.assertEqual(target.read_bytes(), sidecar.read_bytes())
        target.unlink()
        result = self.run_import()
        self.assertEqual((result.resumed, result.sidecars), (1, 1))

    def test_overlapping_trees_and_destination_symlink_rejected(self):
        with self.assertRaises(ValueError):
            with contextlib.redirect_stdout(io.StringIO()):
                import_takeout(self.db, self.root, self.root, self.reader, self.base / 'report.csv', refresh=False)
        self.image(self.source, 'Album/photo.jpg', b'photo')
        (self.root / 'takeout').symlink_to(self.source, target_is_directory=True)
        result = self.run_import()
        self.assertEqual((result.catalogued, result.errors), (0, 1))

    def test_exclusive_temporary_and_publication_preserve_existing_files(self):
        source = self.image(self.source, 'photo.jpg', b'new')
        temporary = self.base / 'temporary.part'
        temporary.write_bytes(b'existing temporary')
        destination = self.root / 'photo.jpg'
        with self.assertRaises(FileExistsError):
            copy_atomic(source, destination, temporary)
        self.assertEqual(temporary.read_bytes(), b'existing temporary')
        destination.write_bytes(b'old photo')
        with self.assertRaises(FileExistsError):
            publish(temporary, destination)
        self.assertEqual(destination.read_bytes(), b'old photo')

    def test_source_changes_during_copy_are_not_published(self):
        source = self.image(self.source, 'photo.jpg', b'photo')
        temporary = self.base / 'temporary.part'
        destination = self.root / 'photo.jpg'
        with self.assertRaises(OSError):
            copy_atomic(source, destination, temporary, lambda amount: source.write_bytes(b'changed'))
        self.assertFalse(destination.exists())
        self.assertFalse(temporary.exists())

    def test_cli_dry_run_and_writer_lock(self):
        self.image(self.source, 'Album/photo.jpg', b'new')
        report = self.base / 'cli.csv'
        with patch('photo_catalogue.ExifTool', return_value=self.reader), patch('photo_catalogue.shutil.which', return_value='/fake/exiftool'):
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(['--db', str(self.db_path), '--log', str(self.base / 'log'), 'import-takeout',
                             str(self.source), str(self.root), '--dry-run', '--report', str(report), '--no-progress'])
        self.assertEqual(code, 0)
        self.assertFalse((self.root / 'takeout').exists())
        with writer_lock(self.db_path):
            with self.assertRaises(ValueError):
                with writer_lock(self.db_path):
                    pass

    def test_dry_run_and_actual_identical_name_collision_agree(self):
        self.image(self.source, 'one/Album/photo.jpg', b'identical')
        self.image(self.source, 'two/Album/photo.jpg', b'identical')
        dry = self.run_import(dry_run=True)
        actual = self.run_import()
        self.assertEqual((dry.planned, dry.recovered), (1, 1))
        self.assertEqual((actual.catalogued, actual.recovered), (1, 1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 1)

    def test_restart_cleans_only_its_journalled_partial_file(self):
        self.image(self.source, 'Album/photo.jpg', b'photo')
        with patch('catalogue.importer.register_copy', side_effect=sqlite3.OperationalError('failure')):
            self.run_import()
        row = self.db.execute('SELECT * FROM takeout_imports').fetchone()
        partial = self.root / row['temporary_relative']
        partial.write_bytes(b'left after a simulated power loss')
        untouched = partial.parent / '.popeye-import-unrelated.part'
        untouched.write_bytes(b'not owned by this job')
        result = self.run_import()
        self.assertEqual(result.recovered, 1)
        self.assertFalse(partial.exists())
        self.assertEqual(untouched.read_bytes(), b'not owned by this job')

    def test_root_level_images_use_source_folder_as_parent(self):
        self.image(self.source, 'photo.jpg', b'photo')
        self.assertEqual(self.run_import().catalogued, 1)
        self.assertTrue((self.root / 'takeout/Google Photos/photo.jpg').is_file())



class RealExifToolTests(unittest.TestCase):
    def test_end_to_end_with_real_jpeg_exif(self):
        import shutil
        import subprocess
        from PIL import Image
        if not shutil.which('exiftool'):
            self.skipTest('System ExifTool is not installed')
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, source = base / 'local', base / 'Google Photos'
            root.mkdir()
            (source / 'Holiday').mkdir(parents=True)
            first = root / 'original.jpg'
            second = source / 'Holiday' / 'new é.jpg'
            for path, date in ((first, '2025:05:01 12:00:00'), (second, '2025:05:02 12:00:00')):
                Image.new('RGB', (40, 30), 'green').save(path)
                subprocess.run(['exiftool', '-overwrite_original', '-Make=Test', '-Model=Camera',
                                '-DateTimeOriginal=' + date, '-SubSecTimeOriginal=123',
                                '-OffsetTimeOriginal=+01:00', str(path)], check=True, capture_output=True)
            shutil.copy2(first, source / 'Holiday' / 'duplicate.jpg')
            common = ['--db', str(base / 'db.sqlite3'), '--log', str(base / 'log')]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([*common, 'scan', str(root), '--no-progress']), 0)
                self.assertEqual(main([*common, 'import-takeout', str(source), str(root), '--dry-run',
                                       '--report', str(base / 'dry.csv'), '--no-progress']), 0)
                self.assertFalse((root / 'takeout').exists())
                self.assertEqual(main([*common, 'import-takeout', str(source), str(root),
                                       '--report', str(base / 'real.csv'), '--no-progress']), 0)
            self.assertEqual((root / 'takeout/Holiday/new é.jpg').read_bytes(), second.read_bytes())
            self.assertFalse((root / 'takeout/Holiday/duplicate.jpg').exists())
            db = connect(base / 'db.sqlite3')
            try:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 2)
                row = db.execute("SELECT * FROM photos WHERE filename='new é.jpg'").fetchone()
                self.assertEqual(row['captured_utc'], '2025-05-02 11:00:00.123000')
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
