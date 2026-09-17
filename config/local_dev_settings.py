"""Settings for a LOCAL development database — a real PostgreSQL you can keep.

The other four modules each refuse to be this one:

* ``sqlite_test_settings`` throws its database away and disables migrations.
* ``sqlite_makemigrations_settings`` writes to a fresh temp file every run, so
  nothing you load survives to the next command.
* ``pgtest_settings`` is real PostgreSQL but drives Django's *test* database,
  created and dropped around a run.
* the default reads ``.env``, and ``.env`` points at **production**.

So there was nowhere to put a local copy of the directory and work on it. This
is that place: the ``factory-pgtest`` container on 127.0.0.1:55432, database
``factory_local``, migrations enabled, persistent between commands. Same engine
as production (PG 16), so the migration chain and every PG-only constraint are
exercised for real rather than approximated on SQLite.

Never points at a live host, and deliberately ignores ``.env``'s ``DB_*`` so a
stale shell cannot redirect it at one.

    docker start factory-pgtest
    python manage.py migrate --settings=config.local_dev_settings
    python manage.py runserver --settings=config.local_dev_settings
"""
import tempfile

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "factory_local",
        "USER": "factory",
        "PASSWORD": "test",
        "HOST": "127.0.0.1",
        "PORT": "55432",
    }
}
# The side databases are production hosts read from .env; a local sandbox has
# no business opening them.
DATABASES.pop("ai_readonly", None)

MEDIA_ROOT = tempfile.mkdtemp(prefix="factory-local-media-")
DEBUG = True
