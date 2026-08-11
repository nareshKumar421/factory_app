"""Read orders out of the OMS PostgreSQL database.

Raw SQL, not the ORM, on purpose: OMS's schema is not ours to model, and a Django
model mapped onto someone else's tables invites a migration to be generated
against them. Every statement here is a SELECT.

Column names are taken from the live schema (inspected 11 Aug 2026), not from the
written specification, which differs in several places:

  * ``delivery_date`` is **text**, not a date
  * ``company`` is a **varchar** holding ``'1'`` / ``'2'``
  * ``created_by`` / ``approved_by`` are ``created_by_id`` / ``approved_by_id``
  * ``is_auto_free`` and ``combo_source_code`` **do not exist**
  * ``sales_*_logs.order_id`` is a **varchar**, so any join needs a numeric guard

Safety: the connection is opened read-only, and
:class:`order_processing.routers.OmsReadOnlyRouter` refuses writes and migrations
on this alias independently.
"""
import logging
from contextlib import contextmanager

from django.conf import settings
from django.db import connections

logger = logging.getLogger(__name__)

OMS_ALIAS = "oms_orders"


class OmsUnavailable(Exception):
    """OMS could not be reached or read. Never raised for 'no new orders'."""


def is_configured():
    return bool(getattr(settings, "OMS_DB_NAME", "")) and OMS_ALIAS in settings.DATABASES


@contextmanager
def oms_cursor():
    """A read-only cursor on the OMS database.

    ``SET TRANSACTION READ ONLY`` is issued explicitly: even holding a superuser
    credential, a write from this code path should fail at the server, not merely
    be absent by convention.
    """
    if not is_configured():
        raise OmsUnavailable(
            "OMS database is not configured. Set OMS_DB_NAME and its credentials."
        )
    conn = connections[OMS_ALIAS]
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            yield cur
    except OmsUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — every OMS failure looks the same to callers
        logger.warning("OMS read failed: %s", exc)
        raise OmsUnavailable(str(exc)) from exc


def _rows(cur):
    columns = [c[0] for c in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


# Explicit column lists rather than SELECT *: an OMS schema change should surface
# as a clear error here, not as a silently missing field three layers downstream.
ORDER_COLUMNS = """
    o.id, o.order_number, o.card_code, o.card_name, o.company,
    o.dispatch_from_id, o.dispatch_from_name, o.po_number, o.ship_to_address,
    o.total_amount, o.is_foc, o.remarks, o.order_type, o.employee_id,
    o.delivery_date, o.sap_created, o.sap_doc_number, o.quotation_cancelled,
    o.created_at, o.updated_at, s.code AS status_code
"""

LINE_COLUMNS = """
    i.id, i.order_id, i.item_code, i.item_name, i.category, i.brand, i.sub_group,
    i.qty, i.pcs, i.boxes, i.ltrs, i.qty_scheme, i.basic_price, i.total
"""


def fetch_orders(since=None, limit=None, statuses=None, order_ids=None):
    """Order headers, newest-changed first, for incremental sync.

    ``since`` is compared with ``>`` not ``>=``: the previous run's high-water mark
    has already been consumed, and re-reading it every time would re-process the
    same order forever on a quiet system.
    """
    from .mapper import to_naive_oms

    where, params = ["1=1"], []
    if since is not None:
        # orders.updated_at is `timestamp WITHOUT time zone`; handing it an aware
        # value raises in the driver, so the watermark goes back as OMS wall clock.
        where.append("o.updated_at > %s")
        params.append(to_naive_oms(since))
    if statuses:
        where.append("s.code = ANY(%s)")
        params.append(list(statuses))
    if order_ids:
        where.append("o.id = ANY(%s)")
        params.append(list(order_ids))

    sql = f"""
        SELECT {ORDER_COLUMNS}
        FROM orders o
        LEFT JOIN order_statuses s ON s.id = o.status_id
        WHERE {' AND '.join(where)}
        ORDER BY o.updated_at ASC, o.id ASC
    """
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))

    with oms_cursor() as cur:
        cur.execute(sql, params)
        return _rows(cur)


def fetch_lines(order_ids):
    """Lines for the given orders, in one round trip."""
    if not order_ids:
        return []
    with oms_cursor() as cur:
        cur.execute(
            f"SELECT {LINE_COLUMNS} FROM order_items i "
            "WHERE i.order_id = ANY(%s) ORDER BY i.order_id, i.id",
            [list(order_ids)],
        )
        return _rows(cur)


def fetch_statuses():
    with oms_cursor() as cur:
        cur.execute("SELECT id, code, name FROM order_statuses ORDER BY id")
        return _rows(cur)


def fetch_sap_documents(order_ids):
    """What SAP actually received, per order — quotations and sales orders.

    Reads both log tables because OMS switched from Quotations to Sales Orders in
    July 2026, and orders either side of that boundary are still live. Only Sales
    Orders commit stock in SAP, so which one was posted decides whether we may
    treat the demand as already reserved.

    ``order_id`` is a varchar in both tables, hence the ``~ '^[0-9]+$'`` guard: a
    non-numeric value would abort the whole statement on cast.
    """
    if not order_ids:
        return []
    sql = """
        SELECT order_id::integer AS order_id, 'QUOTATION' AS doc_type,
               sap_doc_entry, sap_doc_num, status, created_at
        FROM sales_quotation_logs
        WHERE order_id ~ '^[0-9]+$' AND order_id::integer = ANY(%s)
        UNION ALL
        SELECT order_id::integer, 'SALES_ORDER',
               sap_doc_entry, sap_doc_num, status, created_at
        FROM sales_orders_logs
        WHERE order_id ~ '^[0-9]+$' AND order_id::integer = ANY(%s)
        ORDER BY created_at DESC
    """
    with oms_cursor() as cur:
        cur.execute(sql, [list(order_ids), list(order_ids)])
        return _rows(cur)


def max_updated_at():
    """The newest ``orders.updated_at`` in OMS — the ceiling for any sync."""
    with oms_cursor() as cur:
        cur.execute("SELECT max(updated_at) FROM orders")
        return cur.fetchone()[0]


def ping():
    """Cheap health check. Returns ``(ok, detail)`` and never raises."""
    try:
        with oms_cursor() as cur:
            cur.execute("SELECT count(*) FROM orders")
            return True, f"{cur.fetchone()[0]} orders visible"
    except OmsUnavailable as exc:
        return False, str(exc)
