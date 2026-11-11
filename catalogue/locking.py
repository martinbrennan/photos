"""Cooperative single-writer lock; browser readers remain independent."""
import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def writer_lock(database):
    path = Path(str(Path(database).resolve()) + '.writer.lock')
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another scan/import is using this catalogue; wait for it to finish') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
