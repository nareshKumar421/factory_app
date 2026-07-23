"""SQLite test settings.

Same as :mod:`config.test_settings` (migrations disabled -> schema built from
models) but forces an isolated in-memory SQLite database so unit tests run
without a live Postgres/HANA.
"""
from .test_settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Drop the optional read-only AI DB if the base settings configured one.
DATABASES.pop("ai_readonly", None)
