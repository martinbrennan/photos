import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalogue.database import connect
from catalogue.metadata import ExifTool, timestamps
from catalogue.scanner import scan, validate_root
from photo_catalogue import main


class Reader:
    def __init__(self):
        self.calls = []

    def read(self, paths):
        self.calls.extend(paths)
        return {p: {'ImageWidth': 9000, 'ImageHeight': 6000,
                    'DateTimeOriginal': '2026:10:27 14:25:00', 'SubSecTimeOriginal': '123456789',
                    'OffsetTimeOriginal': '+01:00', 'Make': 'Camera'} for p in paths}


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'photos'
        self.root.mkdir()
        self.db_path = self.base / 'catalogue.sqlite3'
        self.db = connect(self.db_path)
        self.reader = Reader()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def run_scan(self, reader=None, batch_size=2):
        return scan(self.db, self.root, reader or self.reader, batch_size, False)

    def test_incremental_and_stable_identity(self):
        path = self.root / 'space é.jpg'
        path.write_bytes(b'photo')
        (self.root / 'nested').mkdir()
        (self.root / 'nested' / path.name).write_bytes(b'other')
        first = self.run_scan()
        self.assertEqual(first.catalogued, 2)
        rows = self.db.execute('SELECT * FROM photos ORDER BY id').fetchall()
        self.assertNotEqual(rows[0]['stable_id'], rows[1]['stable_id'])
        second = self.run_scan()
        self.assertEqual(second.skipped, 2)
        self.assertEqual(len(self.reader.calls), 2)
        path.write_bytes(b'changed photo')
        third = self.run_scan()
        self.assertEqual((third.catalogued, third.skipped), (1, 1))
        updated = self.db.execute('SELECT * FROM photos WHERE id=?', (rows[0]['id'],)).fetchone()
        self.assertEqual(updated['stable_id'], rows[0]['stable_id'])
        self.assertEqual(updated['first_catalogued_at'], rows[0]['first_catalogued_at'])
        self.assertEqual(updated['subsecond_original'], '123456789')
        path.unlink()
        self.run_scan()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 2)

    def test_exclusions_and_missing_metadata(self):
        (self.root / 'one.PNG').write_bytes(b'x')
        (self.root / 'text.txt').write_text('ignore')
        (self.root / 'linked.jpg').symlink_to(self.root / 'one.PNG')
        (self.root / 'loop').symlink_to(self.root, target_is_directory=True)
        with patch.object(self.reader, 'read', side_effect=lambda paths: {p: {} for p in paths}):
            counts = self.run_scan()
        self.assertEqual(counts.catalogued, 1)
        row = self.db.execute('SELECT * FROM photos').fetchone()
        self.assertIsNone(row['captured_local'])
        self.assertIsNotNone(row['chronological_at'])

    def test_corrupt_file_retried(self):
        (self.root / 'bad.jpg').write_bytes(b'bad')
        with patch.object(self.reader, 'read', side_effect=lambda paths: {p: {'Error': 'corrupt'} for p in paths}):
            self.assertEqual(self.run_scan().errors, 1)
        self.assertEqual(self.run_scan().catalogued, 1)

    def test_interrupt_preserves_completed_batches(self):
        for n in range(3):
            (self.root / f'{n}.jpg').write_bytes(b'x')
        original = self.reader.read
        calls = 0

        def interrupted(paths):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return original(paths)

        with patch.object(self.reader, 'read', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_scan(batch_size=1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 1)
        resumed = self.run_scan()
        self.assertEqual((resumed.skipped, resumed.catalogued), (1, 2))

    def test_mount_validation(self):
        with self.assertRaises(ValueError):
            validate_root(self.root, self.root)
        with self.assertRaises(FileNotFoundError):
            validate_root(self.base / 'missing')
        with patch('catalogue.scanner.Path.resolve', return_value=Path('/run/media/user/unmounted')), \
             patch('catalogue.scanner.Path.is_dir', return_value=True), \
             patch('catalogue.scanner.Path.is_mount', lambda path: path in (Path('/'), Path('/run'))):
            with self.assertRaises(ValueError):
                validate_root(Path('/run/media/user/unmounted'))

    def test_stats_list_random(self):
        path = self.root / 'one.jpg'
        path.write_bytes(b'x')
        self.run_scan()
        for command, expected in [(['stats'], 'Photographs: 1'), (['random'], str(path)),
                                  (['list', '--from', '2026-10-27 14:00:00', '--to', '2026-10-27 16:00:00'], str(path)),
                                  (['list', '--time-basis', 'utc', '--from', '2026-10-27T13:00:00+00:00',
                                    '--to', '2026-10-27T14:00:00+00:00'], str(path))]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['--db', str(self.db_path), '--log', str(self.base / 'test.log'), *command]), 0)
            self.assertIn(expected, output.getvalue())


class TimestampTests(unittest.TestCase):
    def test_offsets_subseconds(self):
        self.assertEqual(timestamps('2026:10:27 14:00:00', '012345678', '+01:30'),
                         ('2026-10-27 14:00:00.012345', '2026-10-27 12:30:00.012345'))
        self.assertEqual(timestamps('2026:10:27 14:00:00')[1], None)
        self.assertEqual(timestamps('bad'), (None, None))
        self.assertEqual(timestamps('2026:02:30 00:00:00'), (None, None))
        self.assertEqual(timestamps(None), (None, None))

    def test_exiftool_argv_and_partial_failure(self):
        import subprocess
        result = subprocess.CompletedProcess([], 1, '[{"SourceFile":"/a b/é\\n.jpg","ImageWidth":50}]', 'one file failed')
        with patch('catalogue.metadata.subprocess.run', return_value=result) as run:
            records = ExifTool().read(['/a b/é\n.jpg'])
        self.assertEqual(records['/a b/é\n.jpg']['ImageWidth'], 50)
        self.assertEqual(run.call_args.args[0][-1], '/a b/é\n.jpg')


if __name__ == '__main__':
    unittest.main()
