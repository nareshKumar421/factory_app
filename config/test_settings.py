"""Test settings: build the schema straight from models (no migrations).

The project has a few PostgreSQL-only data migrations that a fresh SQLite
``migrate`` cannot replay; disabling migrations lets the test runner create
tables via ``--run-syncdb`` so the marketplace suite runs on an isolated SQLite
DB without a live Postgres/HANA.
"""
from .settings import *  # noqa: F401,F403


class _DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = _DisableMigrations()
