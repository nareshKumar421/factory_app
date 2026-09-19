"""OMS invoice approval, read and decided straight from OMS's own Postgres.

A drop-in for :class:`invoice_approval.oms.OmsClient`: same five operations, same
arguments, same return shapes, same exception types — so the views pick a backend
and nothing else changes, and ``OMS_USE_DATABASE`` can be flipped back without a
deploy. The response shape is the frontend's ``InvoiceLog`` contract, which is
shared with the SAP-source rows and so is not ours to alter.

**Why not just call the API.** OMS throttles per source IP and every factory user
shares the one bucket, because all of this app's traffic leaves a single server.
The sidebar badge polls from every page for every approver, so that background
traffic was spending the quota the approvers' own page loads needed. Here the
badge is a ``COUNT(*)`` against a database with no such quota.

**Three things about this schema that are not ours.**

``invoice_log`` and ``invoice_history`` belong to another application, which
migrates them on its own schedule. ``manage.py check_oms_schema`` asserts that
what this module reads still exists; run it after an OMS release.

Soft deletes are the trap. ``is_deleted`` rows are off OMS's review screen, and
on 2026-09-19 every one of the 42 PENDING rows in the live database was deleted —
a query that forgets the flag resurrects invoices reviewers deliberately cleared,
some of them from July. Every read below filters it, and the write refuses on it.

OMS also scopes its list endpoint to the caller's branches. It does not apply to
us: this app calls OMS anonymously, and ``branches_for_user`` returns "all
branches" for an unauthenticated caller — so no branch filter here is a faithful
port, not an omission. Verified 2026-09-19 against live data: identical id sets
to the API across 15 warehouse/status combinations.
"""
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, connections, transaction
from django.utils import timezone

from .fg_stock import build_fg_stock_map, fg_stock_for_invoice
from .oms import (
    DECISION_STATUSES,
    OMSConnectionError,
    OMSDataError,
    OMSValidationError,
    OmsClient,
)

logger = logging.getLogger(__name__)

OMS_DB_ALIAS = "oms"

# Columns read for a listed invoice. Named rather than SELECT *, so a column
# added on the OMS side cannot silently change the shape we serve, and a column
# removed fails here instead of somewhere further down.
_INVOICE_COLUMNS = (
    "id",
    "so_number",
    "party_name",
    "total_amount",
    "branch",
    "warehouse",
    "status",
    "rejection_reason",
    "error_message",
    "invoice_payload",
    "created_at",
    "created_by_id",
    "sap_doc_num",
    "sap_doc_entry",
    "supersedes_id",
)

_HISTORY_COLUMNS = (
    "id",
    "invoice_log_id",
    "so_number",
    "party_name",
    "total_amount",
    "status",
    "rejection_reason",
    "error_message",
    "created_at",
    "created_by",
    "device_id",
    "device_name",
)


def _decimal_str(value):
    """Decimals as strings, the way DRF rendered them.

    ``total_amount`` is typed ``string | null`` in the frontend because that is
    what came over the wire. Handing it a float instead would change the type of
    a money field mid-flight.
    """
    return None if value is None else str(value)


def _isoformat(value):
    """ISO-8601 the way DRF rendered it, ``Z`` and all.

    ``isoformat()`` alone gives ``+00:00`` where the API gave ``Z``. Both parse,
    but this string is the one the frontend has always received, and a timestamp
    is not the place to find out which consumers were relying on the exact shape.
    """
    if value is None:
        return None
    text = value.isoformat()
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def _json(value):
    """Decode a jsonb column read through a raw cursor.

    ``invoice_payload`` is genuinely ``jsonb``, but the ORM is not involved here
    and so neither is ``JSONField.from_db_value``, which is what normally decodes
    it — Django deliberately leaves Postgres JSON as text for the field to parse.
    A raw cursor therefore hands back the string, and everything downstream that
    expects a mapping breaks on it.

    Both shapes are accepted rather than just the string: which one arrives is a
    property of the database driver, and that is not something this module should
    be pinned to.
    """
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            logger.warning("OMS invoice_payload was not valid JSON; serving it empty")
            return {}
    return value


class OmsDbClient:
    """Reads and decides OMS invoices directly against the OMS database."""

    def __init__(self):
        self.count_cache_ttl = getattr(settings, "OMS_PENDING_COUNT_CACHE_SECONDS", 60)

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(getattr(settings, "OMS_ENABLED", False))

    def _validate_config(self) -> None:
        if OMS_DB_ALIAS not in settings.DATABASES:
            raise OMSValidationError(
                "OMS is set to read from its database, but no OMS database is "
                "configured. Set OMS_DB_NAME (and the other OMS_DB_* keys)."
            )

    def _cursor(self):
        self._validate_config()
        return connections[OMS_DB_ALIAS].cursor()

    # ── Reads ────────────────────────────────────────────────────────────────
    def list_invoices(self, warehouse: str, status: str | None = None) -> list:
        """Invoices for a warehouse, optionally by status.

        Ordered newest first. The API returned no ORDER BY at all, so its order
        was whatever the planner produced and could differ between two identical
        requests; an approval queue reads oldest-to-newest or newest-to-oldest,
        never arbitrarily, so this is a deliberate improvement on the port.
        """
        if not (warehouse or "").strip():
            raise OMSValidationError("warehouse (whs) is required")

        sql = f"""
            SELECT {", ".join(f'"{c}"' for c in _INVOICE_COLUMNS)}
            FROM invoice_log
            WHERE warehouse = %s AND is_deleted = false
        """
        params = [warehouse]
        if status:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC"

        try:
            with self._cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        except DatabaseError as exc:
            logger.error("OMS database read failed (list %s/%s): %s", warehouse, status, exc)
            raise OMSConnectionError("Unable to read from the OMS database") from exc

        invoices = []
        for row in rows:
            invoice = dict(zip(_INVOICE_COLUMNS, row))
            # Before anything reads it: the FG stock lookup walks DocumentLines,
            # and so does the serializer.
            invoice["invoice_payload"] = _json(invoice["invoice_payload"])
            invoices.append(invoice)

        # One HANA round trip for the whole page, not one per invoice — and a
        # HANA outage costs the stock column, not the list.
        stock_map = build_fg_stock_map(invoices)
        return [self._serialize(invoice, stock_map) for invoice in invoices]

    @staticmethod
    def _serialize(invoice: dict, stock_map=None) -> dict:
        """One invoice in the shape the frontend's ``InvoiceLog`` describes.

        The API also returned ``item_names``, ``can_delete``, ``deleted_by_name``
        and the three ``supersedes_*`` lookups. None of them is read by the
        factory's approval page — they serve OMS's own review screen — so they
        are deliberately not rebuilt here.
        """
        return {
            "id": invoice["id"],
            "so_number": invoice["so_number"],
            "party_name": invoice["party_name"],
            "total_amount": _decimal_str(invoice["total_amount"]),
            "branch": invoice["branch"],
            "warehouse": invoice["warehouse"],
            "status": invoice["status"],
            "rejection_reason": invoice["rejection_reason"],
            "error_message": invoice["error_message"],
            "invoice_payload": invoice["invoice_payload"],
            "created_at": _isoformat(invoice["created_at"]),
            "created_by": invoice["created_by_id"],
            "sap_doc_num": invoice["sap_doc_num"],
            "sap_doc_entry": invoice["sap_doc_entry"],
            "supersedes": invoice["supersedes_id"],
            "fg_stock": fg_stock_for_invoice(invoice, stock_map),
        }

    def get_history(self, invoice_id) -> list:
        """The audit trail for one invoice, oldest first.

        A reworked invoice is a chain of logs, not one row: each revision points
        at the rejected one it replaces. The trail follows that chain so the
        reviewer sees one continuous timeline rather than a stump beginning after
        the last rejection.

        ``CYCLE`` is not defensive dressing — a bad backfill that made the chain
        loop would otherwise spin this query until the connection died. Postgres
        stops it at the repeat instead.
        """
        chain_sql = """
            WITH RECURSIVE chain AS (
                SELECT id, supersedes_id FROM invoice_log WHERE id = %s
                UNION ALL
                SELECT l.id, l.supersedes_id
                FROM invoice_log l
                JOIN chain c ON l.id = c.supersedes_id
            ) CYCLE id SET is_cycle USING path
            SELECT id FROM chain WHERE NOT is_cycle
        """
        history_sql = f"""
            SELECT {", ".join(f'"{c}"' for c in _HISTORY_COLUMNS)}
            FROM invoice_history
            WHERE invoice_log_id = ANY(%s)
            ORDER BY created_at, id
        """
        try:
            with self._cursor() as cursor:
                cursor.execute("SELECT 1 FROM invoice_log WHERE id = %s", [invoice_id])
                if cursor.fetchone() is None:
                    raise OMSDataError(f"OMS invoice {invoice_id} not found")

                cursor.execute(chain_sql, [invoice_id])
                chain_ids = [row[0] for row in cursor.fetchall()]

                cursor.execute(history_sql, [chain_ids])
                rows = cursor.fetchall()
        except DatabaseError as exc:
            logger.error("OMS database read failed (history %s): %s", invoice_id, exc)
            raise OMSConnectionError("Unable to read from the OMS database") from exc

        history = []
        for row in rows:
            record = dict(zip(_HISTORY_COLUMNS, row))
            record["total_amount"] = _decimal_str(record["total_amount"])
            record["created_at"] = _isoformat(record["created_at"])
            # The API declared `created_by_name` but sourced it from `created_by.name`
            # on what is a plain CharField, so DRF dropped it and the field never
            # reached the browser — the trail showed no author at all. It is the
            # name itself, so serve it under both keys: the frontend already reads
            # `created_by_name`, and `created_by` keeps the old shape intact.
            record["created_by_name"] = record["created_by"]
            record["invoice_log"] = record.pop("invoice_log_id")
            history.append(record)
        return history

    # ── Pending count (cached — it is a background poll, not a page read) ─────
    def pending_count(self, warehouse: str) -> int:
        """Number of live PENDING invoices at ``warehouse``.

        A ``COUNT(*)``, where the API path had to pull the whole PENDING list and
        take its length. The cache is kept anyway: the badge polls from every
        page for every approver, and the key is shared with the HTTP client so
        switching backends does not strand a stale count under a second key.
        """
        key = OmsClient._count_cache_key(warehouse)
        cached = cache.get(key)
        if cached is not None:
            return cached

        try:
            with self._cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM invoice_log
                    WHERE warehouse = %s AND is_deleted = false AND status = 'PENDING'
                    """,
                    [warehouse],
                )
                count = cursor.fetchone()[0]
        except DatabaseError as exc:
            logger.error("OMS database read failed (count %s): %s", warehouse, exc)
            raise OMSConnectionError("Unable to read from the OMS database") from exc

        if self.count_cache_ttl:
            cache.set(key, count, timeout=self.count_cache_ttl)
        return count

    @classmethod
    def invalidate_pending_count(cls, warehouse: str) -> None:
        """Drop the cached count so the badge reflects a decision immediately."""
        OmsClient.invalidate_pending_count(warehouse)

    # ── Write ────────────────────────────────────────────────────────────────
    def update_status(
        self,
        invoice_id,
        status: str,
        rejection_reason: str | None = None,
        user: str | None = None,
    ) -> dict:
        """Approve or reject one invoice, and append to its trail.

        The two statements are one transaction on purpose: a status change that
        left no history row would be a decision with no author, on a record SAP
        documents are raised from.

        ``user`` is the approver's display name. OMS's ``created_by`` here is a
        free-text name, not a linked account — which is why the HTTP path never
        actually needed OMS credentials to record a decision, contrary to the
        note that used to sit in settings.py.
        """
        if status not in DECISION_STATUSES:
            raise OMSValidationError("status must be APPROVED or REJECTED")
        if status == "REJECTED" and not (rejection_reason or "").strip():
            raise OMSValidationError("rejection_reason is required when status is REJECTED")

        try:
            with transaction.atomic(using=OMS_DB_ALIAS):
                with self._cursor() as cursor:
                    # Locked for the whole decision: two approvers on the same
                    # invoice must not both read PENDING and both write history.
                    cursor.execute(
                        """
                        SELECT status, is_deleted, error_message
                        FROM invoice_log WHERE id = %s FOR UPDATE
                        """,
                        [invoice_id],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise OMSDataError(f"OMS invoice {invoice_id} not found")

                    _, is_deleted, error_message = row
                    if is_deleted:
                        # OMS answers 409 here. We have no 409 to raise that the
                        # view maps, and the approver's next step is the same
                        # either way: get it restored, then decide.
                        raise OMSValidationError(
                            "This invoice has been deleted. Restore it before "
                            "changing its status."
                        )

                    # InvoiceLog.save() on the OMS side fills in a stand-in
                    # message when a rejection carries no reason. Unreachable
                    # from here — the guard above rejects that combination — but
                    # ported so the row this app writes cannot differ from the row
                    # OMS would have written for the same decision.
                    if status == "REJECTED" and not (rejection_reason or "").strip():
                        error_message = "Invoice rejected without specific reason."

                    cursor.execute(
                        """
                        UPDATE invoice_log
                        SET status = %s, rejection_reason = %s, error_message = %s
                        WHERE id = %s
                        """,
                        [status, rejection_reason, error_message, invoice_id],
                    )

                    # Built by SELECT from the row just updated rather than from
                    # values carried through Python: invoice_payload is jsonb and
                    # round-tripping it through a driver adapter is a chance to
                    # corrupt the record of what was sent to SAP, for no gain.
                    cursor.execute(
                        """
                        INSERT INTO invoice_history (
                            invoice_log_id, so_number, party_name, total_amount,
                            status, rejection_reason, error_message, invoice_payload,
                            created_at, created_by, device_id, device_name
                        )
                        SELECT id, so_number, party_name, total_amount,
                               status, rejection_reason, error_message, invoice_payload,
                               %s, %s, '', ''
                        FROM invoice_log WHERE id = %s
                        """,
                        [timezone.now(), (user or "")[:125], invoice_id],
                    )
        except DatabaseError as exc:
            logger.error("OMS database write failed (status %s): %s", invoice_id, exc)
            raise OMSConnectionError("Unable to write to the OMS database") from exc

        # Same string the HTTP API returned; it is stored on the local audit row.
        return {"message": "Status updated successfully"}


def get_oms_backend():
    """The OMS client the views should use, per ``OMS_USE_DATABASE``.

    One place decides, so the two paths cannot drift apart in the views and a
    revert is a setting rather than a patch.
    """
    if getattr(settings, "OMS_USE_DATABASE", False):
        return OmsDbClient()
    return OmsClient()


def get_oms_backend_class():
    """The class behind :func:`get_oms_backend`, for its classmethods."""
    if getattr(settings, "OMS_USE_DATABASE", False):
        return OmsDbClient
    return OmsClient
