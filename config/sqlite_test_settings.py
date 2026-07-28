"""SQLite test settings.

Same as :mod:`config.test_settings` (migrations disabled -> schema built from
models) but forces an isolated in-memory SQLite database so unit tests run
without a live Postgres/HANA.
"""
import tempfile

from .test_settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Isolate uploaded-file writes (e.g. import raw_file, attachments) to a throwaway
# dir so tests never touch the real media tree.
MEDIA_ROOT = tempfile.mkdtemp(prefix="mp-test-media-")

# Drop the optional read-only AI DB if the base settings configured one.
DATABASES.pop("ai_readonly", None)
