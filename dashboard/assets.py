"""Content-versioned static URLs for assets cached by browsers and the CDN."""
from functools import lru_cache
import hashlib
from pathlib import Path


STATIC_DIR = Path(__file__).resolve().parent / 'static'


@lru_cache(maxsize=128)
def _digest(path, modified, size):
    # Include file metadata in the key so local edits invalidate the digest.
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def static_asset_url(request, filename):
    path = (STATIC_DIR / filename).resolve()
    path.relative_to(STATIC_DIR.resolve())
    stat = path.stat()
    version = _digest(path, stat.st_mtime_ns, stat.st_size)
    # Same-origin paths also work behind TLS-terminating reverse proxies.
    url = request.url_for('static', path=filename).path
    return f'{url}?v={version}'
