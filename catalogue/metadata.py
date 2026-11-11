"""Bounded ExifTool batches: metadata only, never image decoding."""
import json
import re
import subprocess
from datetime import datetime, timezone


def timestamps(original, subsecond=None, offset=None):
    """Preserve EXIF text; normalize sortable local and offset-aware UTC times."""
    if not original:
        return None, None
    try:
        value = str(original)
        value = re.sub(r'^(\d{4}):(\d{2}):(\d{2})', r'\1-\2-\3', value)
        dt = datetime.fromisoformat(value)
        if subsecond is not None:
            digits = str(subsecond).strip()
            if digits.isdigit():
                dt = dt.replace(microsecond=int((digits + '000000')[:6]))
        if dt.tzinfo is None and offset:
            dt = datetime.fromisoformat(dt.isoformat() + str(offset))
        local = dt.replace(tzinfo=None).isoformat(sep=' ', timespec='microseconds')
        utc = (dt.astimezone(timezone.utc).replace(tzinfo=None)
               .isoformat(sep=' ', timespec='microseconds')) if dt.tzinfo else None
        return local, utc
    except (ValueError, TypeError, OverflowError):
        return None, None


class ExifTool:
    def __init__(self, executable='exiftool', timeout=120):
        self.executable = executable
        self.timeout = timeout

    def read(self, paths):
        # Absolute argv paths safely support whitespace, Unicode, newlines and '-'.
        command = [self.executable, '-json', '-n', '-charset', 'filename=UTF8',
                   '-ImageWidth', '-ImageHeight', '-ExifImageWidth', '-ExifImageHeight',
                   '-DateTimeOriginal', '-SubSecTimeOriginal', '-OffsetTimeOriginal',
                   '-Make', '-Model', '-Error', '-Warning', *paths]
        result = subprocess.run(command, capture_output=True, timeout=self.timeout,
                                encoding='utf-8', errors='replace')
        try:
            records = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(result.stderr.strip() or 'Invalid ExifTool output') from exc
        if not isinstance(records, list):
            raise RuntimeError('ExifTool did not return a list')
        return {r['SourceFile']: r for r in records if 'SourceFile' in r}
