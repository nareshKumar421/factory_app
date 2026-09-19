"""Assert that OMS's schema still carries what the direct-database path reads.

``invoice_log`` and ``invoice_history`` belong to another application, which
migrates them on its own schedule and without telling us. This command is the
contract between the two: run it after an OMS release, and in a deployment check,
so a renamed or dropped column is a loud failure here rather than a 500 on the
approver's screen at the moment they try to approve something.

Modelled on ``sync/``'s ``--check`` mode, which guards the same kind of coupling
from the other direction.

    python manage.py check_oms_schema

Exits non-zero when anything it depends on is missing.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections

from invoice_approval.oms_db import (
    _HISTORY_COLUMNS,
    _INVOICE_COLUMNS,
    OMS_DB_ALIAS,
)

# Read by the queries in oms_db but not part of the serialized shape, so they are
# absent from the column tuples above and have to be named here: the soft-delete
# flag every read filters on, and the self-reference the history chain walks.
EXTRA_INVOICE_COLUMNS = ("is_deleted", "supersedes_id")

# Written by update_status. A missing one of these breaks approving, not listing,
# which is the failure nobody notices until an approver is mid-decision.
HISTORY_WRITE_COLUMNS = ("invoice_payload", "device_id", "device_name")

REQUIRED = {
    "invoice_log": sorted(set(_INVOICE_COLUMNS) | set(EXTRA_INVOICE_COLUMNS)),
    "invoice_history": sorted(set(_HISTORY_COLUMNS) | set(HISTORY_WRITE_COLUMNS)),
}


class Command(BaseCommand):
    help = "Verify the OMS database still has the tables and columns this app reads."

    def handle(self, *args, **options):
        if OMS_DB_ALIAS not in settings.DATABASES:
            raise CommandError(
                "No OMS database is configured. Set OMS_DB_NAME and the other "
                "OMS_DB_* keys, or ignore this if the deployment uses the HTTP path."
            )

        try:
            with connections[OMS_DB_ALIAS].cursor() as cursor:
                cursor.execute(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = ANY(%s)
                    """,
                    [list(REQUIRED)],
                )
                found = {}
                for table, column in cursor.fetchall():
                    found.setdefault(table, set()).add(column)
        except DatabaseError as exc:
            raise CommandError(f"Could not read the OMS schema: {exc}") from exc

        problems = []
        for table, columns in REQUIRED.items():
            if table not in found:
                problems.append(f"  table {table!r} is missing entirely")
                continue
            missing = [c for c in columns if c not in found[table]]
            if missing:
                problems.append(f"  {table}: missing {', '.join(missing)}")
            else:
                self.stdout.write(f"  {table}: {len(columns)} columns OK")

        if problems:
            raise CommandError(
                "OMS schema no longer matches what invoice_approval reads:\n"
                + "\n".join(problems)
                + "\n\nUntil this is fixed, set OMS_USE_DATABASE=False to fall back "
                "to the HTTP API."
            )

        self.stdout.write(self.style.SUCCESS("OMS schema contract OK."))
