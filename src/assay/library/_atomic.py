"""Atomic JSON-file replacement so other processes never read partial records."""
import os
import tempfile


def write_text(path, content):
    fd, name = tempfile.mkstemp(prefix='.assay-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
