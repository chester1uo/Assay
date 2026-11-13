"""SQLite WAL connections robust to concurrent first-time initialization."""
import sqlite3
import time


def open_wal(path, *, autocommit=False):
    deadline = time.monotonic() + 30
    while True:
        connection = None
        try:
            options = {'timeout': 30}
            if autocommit:
                options['isolation_level'] = None
            connection = sqlite3.connect(path, **options)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA busy_timeout=30000')
            if connection.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                connection.execute('PRAGMA journal_mode=WAL')
            return connection
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.close()
            if 'locked' not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(.02)
