"""Bills of material and open purchase orders, from SAP.

Table names are taken from what this codebase already uses against the live
system, never invented (Rule 3):

  * BOM components — ``ITT1`` (``Father``, ``Code``, ``Quantity``, ``Warehouse``),
    as used by ``marketplace/management/commands/mp_sync_sale_boms.py``
  * Open purchase orders — ``OPOR``/``POR1`` with ``OpenQty > 0``, as used by
    ``sap_client/hana/po_reader.py``

Two behaviours worth stating:

**A missing BOM is a finding, not an empty list.** Returning ``[]`` for a product
with no recipe would silently report "no materials needed", and the shortfall
would vanish. The caller is told the difference.

**Explosion is single-level by default.** SAP BOMs here nest (a finished good can
list a semi-finished component that has its own recipe), so the depth is bounded
and cycles are refused rather than followed — a self-referencing BOM would
otherwise hang the request.
"""
import logging
from collections import OrderedDict
from decimal import Decimal

logger = logging.getLogger(__name__)

MAX_BOM_DEPTH = 5


class BomUnavailable(Exception):
    """SAP could not be read. Distinct from 'this product has no BOM'."""


def _connect(company_code):
    from hdbcli import dbapi

    from sap_client.context import CompanyContext

    hana = CompanyContext(company_code).hana
    conn = dbapi.connect(
        address=hana["host"], port=int(hana["port"]), user=hana["user"],
        password=hana["password"], encrypt=True, sslValidateCertificate=False,
    )
    return conn, hana["schema"]


def fetch_components(company_code, parent_codes):
    """``{parent: [{item_code, quantity_per_unit, warehouse}, ...]}``.

    A parent absent from the result has no BOM in SAP — which the caller must
    treat as "cannot be made", not "needs nothing".
    """
    codes = sorted({(c or "").strip() for c in parent_codes} - {""})
    if not codes:
        return {}

    conn = None
    try:
        conn, schema = _connect(company_code)
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(codes))
        cur.execute(
            f'SELECT "Father", "Code", "Quantity", IFNULL("Warehouse", \'\') '
            f'FROM "{schema}"."ITT1" WHERE "Father" IN ({placeholders}) '
            f'ORDER BY "Father", "ChildNum"',
            codes,
        )
        out = OrderedDict()
        for father, child, quantity, warehouse in cur.fetchall():
            out.setdefault(father, []).append({
                "item_code": child,
                "quantity_per_unit": Decimal(str(quantity or 0)),
                "warehouse": warehouse or "",
            })
        cur.close()
        return out
    except Exception as exc:  # noqa: BLE001 — environment-specific
        logger.warning("BOM read failed for %s: %s", company_code, exc)
        raise BomUnavailable(str(exc)) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def explode(company_code, parent_code, quantity, *, depth=1):
    """Materials needed to make ``quantity`` of ``parent_code``.

    Returns ``(components, warehouses, missing_bom)``:

      * ``components`` — ``{item_code: quantity}`` summed across the tree
      * ``warehouses`` — ``{item_code: warehouse}`` **as the BOM declares it**
      * ``missing_bom`` — parents that had no recipe

    The warehouse matters more than it looks. ``ITT1.Warehouse`` is the warehouse
    SAP issues that component FROM, and it is not the finished good's warehouse:
    the live BOMs issue every component from ``BH-PC`` while the finished goods
    book against ``GP-FG``. Checking component stock in the FG warehouse finds
    nothing and tells procurement to buy raw material that is sitting in the
    materials store — 67,683 litres of it, in one real case.

    ``depth`` bounds the recursion. A BOM that references itself, directly or
    through a chain, is refused at the limit instead of looping forever.
    """
    totals, warehouses, missing, seen = {}, {}, [], set()

    def walk(code, qty, level):
        if level > max(depth, 1) or code in seen:
            return
        seen.add(code)
        children = fetch_components(company_code, [code]).get(code)
        if not children:
            missing.append(code)
            return
        for child in children:
            needed = qty * child["quantity_per_unit"]
            item = child["item_code"]
            totals[item] = totals.get(item, Decimal("0")) + needed
            # First declaration wins: a component appearing twice in one tree is
            # issued from one place, and disagreeing rows are a BOM problem, not
            # ours to arbitrate.
            warehouses.setdefault(item, child.get("warehouse") or "")
            if level < depth:
                walk(item, needed, level + 1)

    walk(parent_code, Decimal(quantity), 1)
    missing = [m for m in missing if m == parent_code] if not totals else []
    return totals, warehouses, missing


def fetch_open_po_quantities(company_code, item_codes, warehouse_code=None):
    """``{item_code: open quantity}`` from ``POR1.OpenQty``.

    Material already ordered but not yet received. Ignoring it makes the system
    re-order the same thing every cycle until it arrives.
    """
    codes = sorted({(c or "").strip() for c in item_codes} - {""})
    if not codes:
        return {}

    conn = None
    try:
        conn, schema = _connect(company_code)
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(codes))
        params = list(codes)
        where_warehouse = ""
        if warehouse_code:
            where_warehouse = ' AND T1."WhsCode" = ?'
            params.append(warehouse_code)
        cur.execute(
            f'SELECT T1."ItemCode", SUM(T1."OpenQty") '
            f'FROM "{schema}"."OPOR" T0 '
            f'JOIN "{schema}"."POR1" T1 ON T0."DocEntry" = T1."DocEntry" '
            f'WHERE T1."OpenQty" > 0 AND T1."ItemCode" IN ({placeholders})'
            f'{where_warehouse} '
            f'GROUP BY T1."ItemCode"',
            params,
        )
        out = {item: Decimal(str(qty or 0)) for item, qty in cur.fetchall()}
        cur.close()
        return out
    except Exception as exc:  # noqa: BLE001 — environment-specific
        logger.warning("Open-PO read failed for %s: %s", company_code, exc)
        # Best-effort: an unreadable PO list must not stop material planning, but
        # the caller is told so it can mark the figure as unverified.
        raise BomUnavailable(str(exc)) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
