"""Database router that makes the OMS connection structurally read-only.

The credential we hold is a PostgreSQL **superuser** on a host carrying 17
databases, including this application's own. A read-only replica role has been
requested; until it arrives, "we only ever SELECT" must be enforced by the code
rather than by good intentions.

Three layers, deliberately redundant:

1. ``psycopg2``'s ``set_session(readonly=True)`` at connect time (see
   :mod:`order_processing.integrations.oms.reader`).
2. This router, which refuses to route any write or migration to the alias.
3. No Django model is ever mapped to the OMS alias — everything goes through raw
   SELECTs — so there is no ORM path that could write in the first place.

Any one of these is enough. All three means a mistake has to be deliberate.
"""

OMS_ALIAS = "oms_orders"


class OmsReadOnlyRouter:
    """Blocks writes, relations and migrations against the OMS database."""

    def db_for_read(self, model, **hints):
        # Our own models live in the default database; the OMS alias is only ever
        # reached through explicit raw connections, never through model routing.
        return None

    def db_for_write(self, model, **hints):
        return None

    def allow_relation(self, obj1, obj2, **hints):
        # Never permit a ForeignKey to span into OMS: a relation implies a join
        # the OMS server would have to serve, and a cascade it could suffer.
        for obj in (obj1, obj2):
            if getattr(obj, "_state", None) and obj._state.db == OMS_ALIAS:
                return False
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """Never migrate the OMS database. Not our schema to change."""
        if db == OMS_ALIAS:
            return False
        return None
