"""Database router keeping this project out of OMS's schema.

The ``oms`` alias points at another application's production database. We read
it, and we update two of its tables through explicit SQL in :mod:`oms_db` — but
no model here is backed by it, and nothing in this project may ever migrate it.

Django is helpful in exactly the wrong way here: ``migrate`` with no
``--database`` iterates every configured alias, and a test runner will happily
try to build a test database for each one. Either would run this project's
migrations against OMS's schema. ``allow_migrate`` returning False for the alias
is what stops that, and it is the reason this router exists at all.

Reads and writes are deliberately left to Django's default (the ``default``
alias): ORM traffic must never drift onto ``oms`` implicitly. Everything that
genuinely belongs there goes through ``connections["oms"]`` by name.
"""

OMS_DB_ALIAS = "oms"


class OmsDatabaseRouter:
    """Blocks migrations against the OMS alias; leaves everything else alone."""

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """Refuse every migration on the OMS alias, allow the rest.

        Returning ``None`` rather than ``True`` for other aliases leaves the
        decision to Django (and to any router added later) instead of this one
        claiming an answer it has no opinion on.
        """
        if db == OMS_DB_ALIAS:
            return False
        return None

    def allow_relation(self, obj1, obj2, **hints):
        """No opinion — nothing here is modelled against the OMS alias."""
        return None

    def db_for_read(self, model, **hints):
        """No opinion: reads from OMS are explicit, never routed."""
        return None

    def db_for_write(self, model, **hints):
        """No opinion: writes to OMS are explicit, never routed."""
        return None
