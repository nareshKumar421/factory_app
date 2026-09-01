"""Settings for testing against a REAL PostgreSQL server (a throwaway container).

Mirrors production's engine (PG 16) so the MIGRATION CHAIN, the partial unique
constraints and real transaction behaviour are exercised for real — none of which
sqlite_test_settings can prove, because it disables migrations and runs on SQLite.

Points at a local container on 127.0.0.1:55432. Never at a live host.
"""
import tempfile

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "factory_flow",
        "USER": "factory",
        "PASSWORD": "test",
        "HOST": "127.0.0.1",
        "PORT": "55432",
        "TEST": {"NAME": "factory_flow_test"},
    }
}
# Drop the optional side databases — this harness only exercises `default`.
DATABASES.pop("ai_readonly", None)

MEDIA_ROOT = tempfile.mkdtemp(prefix="mp-pgtest-media-")
