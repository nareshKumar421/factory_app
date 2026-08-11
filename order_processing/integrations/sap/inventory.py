"""Finished-goods stock from SAP, for one warehouse at a time.

Reads ``OITW`` through the existing ``sap_client`` per-company registry — no new
connection handling, no new credentials. The columns are the ones this codebase
already uses: ``warehouse/services/wms_hana_reader.py`` computes
``Available = OnHand - IsCommited`` from the same table.

**Why ``IsCommited`` is the whole story here.** SAP populates it from open Sales
Orders. Since July 2026 OMS posts Sales Orders rather than Quotations, so a pushed
OMS order is *already* subtracted inside SAP. Keeping a parallel reservation for
those orders would count the same demand twice. What SAP cannot know about is the
~273 orders that never reached it — those are netted off locally instead, in
:mod:`order_processing.services.availability`.

**Unknown is not zero.** When HANA is unreachable the result says so rather than
returning 0, because a zero would read as "nothing in stock" and could trigger
production for goods sitting in the warehouse.
"""
import logging
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass
class StockLine:
    """What SAP holds for one item in one warehouse."""

    item_code: str
    warehouse_code: str
    on_hand: Decimal = Decimal("0")
    committed: Decimal = Decimal("0")
    on_order: Decimal = Decimal("0")
    known: bool = True

    @property
    def available(self):
        """``OnHand - IsCommited``, floored at zero.

        SAP can report a negative when committed exceeds stock; as an
        *availability* answer that is still simply "none".
        """
        return max(self.on_hand - self.committed, Decimal("0"))


@dataclass
class StockSnapshot:
    """One reading of SAP stock, with enough context to judge how much to trust it."""

    warehouse_code: str
    company_code: str
    lines: dict = field(default_factory=dict)
    available_at: object = None
    error: str = ""

    @property
    def ok(self):
        return not self.error

    def get(self, item_code):
        return self.lines.get(item_code) or StockLine(
            item_code=item_code, warehouse_code=self.warehouse_code, known=False,
        )


def _hana_config(company_code):
    from sap_client.context import CompanyContext

    return CompanyContext(company_code).hana


def fetch_stock(company_code, item_codes, warehouse_code):
    """``StockSnapshot`` for the given items in one warehouse.

    Never raises: a stock check that explodes takes the whole order screen with
    it. Failure is reported on the snapshot so the caller can show "unknown"
    rather than a fabricated number.
    """
    from django.utils import timezone

    codes = sorted({(c or "").strip() for c in item_codes} - {""})
    snapshot = StockSnapshot(
        warehouse_code=warehouse_code, company_code=company_code,
        available_at=timezone.now(),
    )
    if not codes:
        return snapshot
    if not warehouse_code:
        # BEVERAGES lines arrive here: OMS sends no WarehouseCode for them, so
        # there is nowhere to look. Say that plainly instead of guessing.
        snapshot.error = "No warehouse for these lines — cannot check stock."
        return snapshot

    try:
        from hdbcli import dbapi

        hana = _hana_config(company_code)
    except Exception as exc:  # noqa: BLE001 — environment-specific
        logger.warning("SAP stock unavailable for %s: %s", company_code, exc)
        snapshot.error = f"SAP not configured for {company_code}: {exc}"
        return snapshot

    placeholders = ",".join(["?"] * len(codes))
    sql = (
        f'SELECT "ItemCode", "OnHand", "IsCommited", "OnOrder" '
        f'FROM "{hana["schema"]}"."OITW" '
        f'WHERE "WhsCode" = ? AND "ItemCode" IN ({placeholders})'
    )

    conn = None
    try:
        conn = dbapi.connect(
            address=hana["host"], port=int(hana["port"]), user=hana["user"],
            password=hana["password"], encrypt=True, sslValidateCertificate=False,
        )
        cur = conn.cursor()
        cur.execute(sql, [warehouse_code, *codes])
        for item, on_hand, committed, on_order in cur.fetchall():
            snapshot.lines[item] = StockLine(
                item_code=item, warehouse_code=warehouse_code,
                on_hand=Decimal(str(on_hand or 0)),
                committed=Decimal(str(committed or 0)),
                on_order=Decimal(str(on_order or 0)),
            )
        cur.close()
    except Exception as exc:  # noqa: BLE001 — environment-specific
        logger.warning("SAP stock query failed (%s / %s): %s", company_code, warehouse_code, exc)
        snapshot.error = str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # An item SAP has never seen in this warehouse has no OITW row at all. That is
    # different from "zero on hand", and `known=False` keeps the two apart.
    for code in codes:
        snapshot.lines.setdefault(
            code, StockLine(item_code=code, warehouse_code=warehouse_code, known=False)
        )
    return snapshot
