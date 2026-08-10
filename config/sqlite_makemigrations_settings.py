"""Settings for AUTHORING migrations safely.

``config.sqlite_test_settings`` disables migrations (schema is built straight
from models), so ``makemigrations`` refuses to run under it. The default settings
DO allow it but point at the **production** database, and no migration authoring
should go anywhere near that.

This module is the safe middle: the real app list, an isolated on-disk SQLite
database, and migrations left ENABLED so ``makemigrations`` can write files.
It is a developer tool — never a runtime or test setting.

    python manage.py makemigrations <app> --settings=config.sqlite_makemigrations_settings
"""
import tempfile

from .test_settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": tempfile.mktemp(prefix="makemigrations-", suffix=".sqlite3"),
    }
}
DATABASES.pop("ai_readonly", None)

# test_settings switches this out to skip migrations; put it back so the
# autodetector will write migration files.
try:
    del MIGRATION_MODULES  # noqa: F821
except NameError:
    pass
