"""Every SAP read the Live Trail needs, and nothing else.

The trail follows one chain — open order -> finished-goods stock -> work orders
-> bill of materials -> component stock and open POs -> what must be bought —
and each link is a separate table in SAP B1. This module owns all of that SQL so
the service that assembles the trail stays pure Python and can be tested without
HANA.

Two things about this read are deliberate and easy to get wrong:

**One connection, many schemas.** JIVO Oil, Mart and Beverages are three
databases behind one HANA login (see ``sap_client.registry``: same host, same
user, different ``schema``), so demand can be read from Mart and production from
Oil on a single connection simply by qualifying the table name. There is no
cross-company join to arrange and no second login to hold open.

**Quantities are pieces.** ``RDR1.Quantity``/``OpenQty`` are single bottles, not
cartons — ``InvntryUom`` is PCS and ``NumInSale`` is 1. The "20 PCS" in an item
name is carton configuration only; multiplying by it inflates volume ~20x.
"""
import logging
from typing import Any, Dict, Iterable, List, Sequence

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection
from sap_client.registry import get_company_config

logger = logging.getLogger(__name__)

# A bill of materials line is either an item (Type 4) or a production resource
# (Type 290). Resources are the filling/conversion cost line — real money, but
# not something anybody can raise a purchase order for, so they are carried
# through the trail and then excluded from the buy list.
BOM_TYPE_ITEM = 4
BOM_TYPE_RESOURCE = 290

# PDN1.BaseType for "this goods receipt line came from a purchase order line".
BASE_TYPE_PURCHASE_ORDER = 22

# Work orders that still owe production: Planned or Released. Closed and
# Cancelled owe nothing.
OPEN_WORK_ORDER_STATUSES = ("P", "R")

# How far back measured lead times are taken from. Long enough to average out a
# seasonal supplier, short enough that a lead time from three years ago does not
# still shape today's alarm.
LEAD_TIME_MONTHS = 18


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _chunks(values: Sequence[str], size: int = 500) -> Iterable[Sequence[str]]:
    """HANA takes a large IN list happily; a 20,000-item one is another matter."""
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _num(value) -> float:
    return float(value or 0)


def _date(value):
    if not value:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    return text or None


class LiveTrailReader:
    """Reads the whole trail for one production company plus its demand books."""

    def __init__(self, production_company: str, demand_companies: Sequence[str]):
        self.production_company = production_company
        self.demand_companies = list(demand_companies)
        config = get_company_config(production_company)
        self.connection = HanaConnection(config["hana"])
        self.schemas = {
            company: get_company_config(company)["hana"]["schema"]
            for company in {production_company, *demand_companies}
        }
        # Books this environment cannot read, and why. An environment whose
        # config still points a company at a decommissioned schema must not
        # quietly return a smaller order book that looks healthy — the missing
        # demand is carried through to the payload and said out loud.
        self.unavailable_books = {}
        self._conn = None

    @property
    def production_schema(self) -> str:
        return self.schemas[self.production_company]

    # ── connection ────────────────────────────────────────────────────────────

    def __enter__(self):
        try:
            self._conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed for the supply-chain live trail: %s", e)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e
        self._drop_unreadable_books()
        return self

    def _drop_unreadable_books(self):
        """Keep only the demand books this environment can actually read.

        Checked once against the catalog rather than discovered halfway through
        the trail, so a mis-configured company fails as a stated gap in the
        payload instead of a 500 that loses the books that were fine.
        """
        wanted = sorted(set(self.schemas.values()))
        rows = self._rows(
            f'SELECT "SCHEMA_NAME" FROM "SYS"."SCHEMAS" WHERE "SCHEMA_NAME" IN ({_placeholders(wanted)})',
            wanted,
        )
        present = {_text(row[0]) for row in rows}
        if self.schemas[self.production_company] not in present:
            raise SAPDataError(
                f"The production company's SAP schema "
                f"({self.schemas[self.production_company]}) is not readable, so "
                f"nothing can be planned against it."
            )
        readable = []
        for company in self.demand_companies:
            schema = self.schemas[company]
            if schema in present:
                readable.append(company)
            else:
                self.unavailable_books[company] = (
                    f"SAP schema {schema} is not readable from this environment, "
                    f"so this order book is missing from the trail."
                )
                logger.warning("Live trail: skipping %s — schema %s not readable",
                               company, schema)
        self.demand_companies = readable

    def __exit__(self, *exc):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - closing must never mask the real error
                pass
            self._conn = None
        return False

    def _rows(self, query: str, params: Sequence[Any] = ()) -> List[Sequence[Any]]:
        cursor = None
        try:
            cursor = self._conn.cursor()
            cursor.execute(query, list(params))
            return cursor.fetchall()
        except dbapi.ProgrammingError as e:
            logger.error("SAP HANA live-trail query error: %s", e)
            raise SAPDataError("Failed to read the supply chain from SAP. Invalid query.") from e
        except dbapi.Error as e:
            logger.error("SAP HANA live-trail data error: %s", e)
            raise SAPDataError("Failed to read the supply chain from SAP. Please try again.") from e
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:  # pragma: no cover
                    pass

    def _rows_for_codes(self, query_for: str, codes: Sequence[str], *, extra: Sequence[Any] = ()):
        """Run one IN-list query per chunk of codes and concatenate the rows."""
        out: List[Sequence[Any]] = []
        for chunk in _chunks(list(codes)):
            out.extend(self._rows(query_for.format(codes=_placeholders(chunk)),
                                  list(extra) + list(chunk)))
        return out

    # ── stage 1: the order book ───────────────────────────────────────────────

    def open_order_lines(self) -> List[Dict[str, Any]]:
        """Every open sales-order line across the demand companies.

        A line is open when its own ``LineStatus`` is 'O' and it still owes
        quantity. The header's ``DocStatus`` is deliberately not tested as well:
        on this data it selects exactly the same lines, and a header-only test
        would silently drop a part-open line on a header SAP has already closed.
        """
        lines: List[Dict[str, Any]] = []
        for company in self.demand_companies:
            schema = self.schemas[company]
            rows = self._rows(f"""
                SELECT H."DocNum", H."DocEntry", H."CardCode", IFNULL(H."CardName", ''),
                       H."DocDate", H."DocDueDate",
                       L."LineNum", L."ItemCode", IFNULL(L."Dscription", ''),
                       IFNULL(L."Quantity", 0), IFNULL(L."OpenQty", 0), IFNULL(L."Price", 0)
                FROM "{schema}"."ORDR" H
                JOIN "{schema}"."RDR1" L ON L."DocEntry" = H."DocEntry"
                WHERE H."CANCELED" = 'N' AND L."LineStatus" = 'O' AND L."OpenQty" > 0
                ORDER BY H."DocDate", H."DocNum", L."LineNum"
            """)
            for row in rows:
                quantity, open_qty = _num(row[9]), _num(row[10])
                lines.append({
                    "company": company,
                    "doc": int(row[0] or 0),
                    "entry": int(row[1] or 0),
                    "card": _text(row[2]),
                    "party": _text(row[3]),
                    "ordered": _date(row[4]),
                    "due": _date(row[5]),
                    "line": int(row[6] or 0),
                    "item": _text(row[7]),
                    "name": _text(row[8]),
                    "qty": quantity,
                    "open": open_qty,
                    "delivered": max(quantity - open_qty, 0),
                    "price": _num(row[11]),
                })
        return lines

    def demand_item_names(self, company: str, codes: Sequence[str]) -> Dict[str, str]:
        """Item names as *that book's own* master states them.

        Each company database keeps its own item master, and the two number
        spaces diverged at some point: ``FG0000402`` is a 1 LTR sunflower bottle
        in Mart and a 200 LTR groundnut drum in Oil. Reading Mart's demand
        against Oil's master by item code alone therefore plans the wrong
        product, so the name each book holds is fetched and compared.
        """
        if not codes:
            return {}
        schema = self.schemas[company]
        rows = self._rows_for_codes(f"""
            SELECT "ItemCode", IFNULL("ItemName", '')
            FROM "{schema}"."OITM"
            WHERE "ItemCode" IN ({{codes}})
        """, codes)
        return {_text(row[0]): _text(row[1]) for row in rows}

    def production_codes_for_names(self, names: Sequence[str]) -> Dict[str, List[str]]:
        """``{item name: [production codes]}`` — how a foreign code is resolved."""
        if not names:
            return {}
        rows = self._rows_for_codes(f"""
            SELECT UPPER(TRIM("ItemName")), "ItemCode"
            FROM "{self.production_schema}"."OITM"
            WHERE UPPER(TRIM("ItemName")) IN ({{codes}})
        """, [n.upper().strip() for n in names])
        out: Dict[str, List[str]] = {}
        for row in rows:
            out.setdefault(_text(row[0]), []).append(_text(row[1]))
        return out

    # ── stage 2: what is already on the shelf ─────────────────────────────────

    def stock_on_hand(self, company: str, codes: Sequence[str]) -> Dict[str, Dict[str, float]]:
        """``OITW`` on hand and committed, summed across every warehouse."""
        if not codes:
            return {}
        schema = self.schemas[company]
        rows = self._rows_for_codes(f"""
            SELECT "ItemCode", SUM(IFNULL("OnHand", 0)), SUM(IFNULL("IsCommited", 0))
            FROM "{schema}"."OITW"
            WHERE "ItemCode" IN ({{codes}})
            GROUP BY "ItemCode"
        """, codes)
        return {
            _text(row[0]): {"onhand": _num(row[1]), "committed": _num(row[2])}
            for row in rows
        }

    # ── stage 3: what the floor is already making ─────────────────────────────

    def open_work_orders(self, codes: Sequence[str]) -> Dict[str, Dict[str, float]]:
        """Remaining quantity on Planned/Released production orders, per item."""
        if not codes:
            return {}
        statuses = _placeholders(OPEN_WORK_ORDER_STATUSES)
        rows = self._rows_for_codes(f"""
            SELECT "ItemCode",
                   SUM(GREATEST(IFNULL("PlannedQty", 0) - IFNULL("CmpltQty", 0), 0)),
                   COUNT(*)
            FROM "{self.production_schema}"."OWOR"
            WHERE "Status" IN ({statuses})
              AND IFNULL("PlannedQty", 0) - IFNULL("CmpltQty", 0) > 0
              AND "ItemCode" IN ({{codes}})
            GROUP BY "ItemCode"
        """, codes, extra=OPEN_WORK_ORDER_STATUSES)
        return {
            _text(row[0]): {"wip": _num(row[1]), "wo_count": int(row[2] or 0)}
            for row in rows
        }

    # ── stage 4: the bill of materials ────────────────────────────────────────

    def bills_of_material(self, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """One level of the live BOM for each parent that has one.

        ``OITT."Qauntity"`` — SAP's own spelling — is the batch the child
        quantities are stated against, so a per-unit figure needs the division.
        A BOM defined for 20 bottles and read as if for one overstates every
        component twentyfold.
        """
        if not codes:
            return {}
        rows = self._rows_for_codes(f"""
            SELECT T."Code", IFNULL(T."Qauntity", 1),
                   C."Code", IFNULL(C."Quantity", 0), IFNULL(C."Type", {BOM_TYPE_ITEM}),
                   IFNULL(C."Price", 0), C."ChildNum"
            FROM "{self.production_schema}"."OITT" T
            JOIN "{self.production_schema}"."ITT1" C ON C."Father" = T."Code"
            WHERE T."Code" IN ({{codes}})
            ORDER BY T."Code", C."ChildNum"
        """, codes)
        boms: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            parent = _text(row[0])
            base = _num(row[1]) or 1.0
            bom = boms.setdefault(parent, {"base_qty": base, "lines": []})
            bom["lines"].append({
                "child": _text(row[2]),
                "bom_qty": _num(row[3]),
                "bom_base": base,
                "per_unit": _num(row[3]) / base if base else 0.0,
                "is_resource": int(row[4] or 0) == BOM_TYPE_RESOURCE,
                "bom_price": _num(row[5]),
            })
        return boms

    # ── item master, resources, vendors ───────────────────────────────────────

    def item_master(self, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Name, group, variety, unit, minimum level and last purchase price.

        ``U_TYPE`` and ``U_Sub_Group`` are how JIVO segments its range. Matching
        on the item NAME instead is the classic mistake here — COLD PRESS 1 LTR
        is SAP-tagged CANOLA and carries no 'canola' in its name.
        """
        if not codes:
            return {}
        rows = self._rows_for_codes(f"""
            SELECT I."ItemCode", IFNULL(I."ItemName", ''), IFNULL(B."ItmsGrpNam", ''),
                   IFNULL(I."InvntryUom", ''), IFNULL(I."MinLevel", 0),
                   IFNULL(I."LastPurPrc", 0), IFNULL(I."U_TYPE", ''),
                   IFNULL(I."U_Sub_Group", ''), IFNULL(I."PrchseItem", 'N')
            FROM "{self.production_schema}"."OITM" I
            LEFT JOIN "{self.production_schema}"."OITB" B ON B."ItmsGrpCod" = I."ItmsGrpCod"
            WHERE I."ItemCode" IN ({{codes}})
        """, codes)
        return {
            _text(row[0]): {
                "name": _text(row[1]),
                "group": _text(row[2]),
                "uom": _text(row[3]),
                "min_level": _num(row[4]),
                "price": _num(row[5]),
                "type": _text(row[6]) or "-",
                "variety": _text(row[7]) or "-",
                "purchased": _text(row[8]).upper() == "Y",
            }
            for row in rows
        }

    def resources(self) -> Dict[str, Dict[str, Any]]:
        """Production resources and their standard rate (``StdCost1``)."""
        rows = self._rows(f"""
            SELECT "ResCode", IFNULL("ResName", ''), IFNULL("StdCost1", 0), "UnitOfMsr"
            FROM "{self.production_schema}"."ORSC"
        """)
        return {
            _text(row[0]): {
                "name": _text(row[1]),
                "rate": _num(row[2]),
                # ORSC carries no unit for JIVO's filling resources; they are all
                # charged per litre, which is what the BOM quantity states.
                "uom": _text(row[3]) or "LTR",
            }
            for row in rows
        }

    def last_vendors(self, codes: Sequence[str]) -> Dict[str, str]:
        """Who we last bought each component from — the call procurement makes."""
        if not codes:
            return {}
        rows = self._rows_for_codes(f"""
            SELECT L."ItemCode", H."CardName", H."DocDate"
            FROM "{self.production_schema}"."POR1" L
            JOIN "{self.production_schema}"."OPOR" H ON H."DocEntry" = L."DocEntry"
            WHERE H."CANCELED" = 'N' AND L."ItemCode" IN ({{codes}})
            ORDER BY L."ItemCode", H."DocDate" DESC
        """, codes)
        vendors: Dict[str, str] = {}
        for row in rows:
            vendors.setdefault(_text(row[0]), _text(row[1]))
        return vendors

    # ── stage 5: supply already on order ──────────────────────────────────────

    def open_purchase_lines(self, codes: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Open PO lines per component, with the line's own expected date."""
        if not codes:
            return {}
        rows = self._rows_for_codes(f"""
            SELECT L."ItemCode", H."DocEntry", H."DocNum", IFNULL(H."CardName", ''),
                   L."ShipDate", H."DocDate", IFNULL(L."OpenQty", 0), IFNULL(L."Price", 0)
            FROM "{self.production_schema}"."OPOR" H
            JOIN "{self.production_schema}"."POR1" L ON L."DocEntry" = H."DocEntry"
            WHERE H."CANCELED" = 'N' AND L."LineStatus" = 'O' AND L."OpenQty" > 0
              AND L."ItemCode" IN ({{codes}})
        """, codes)
        out: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(_text(row[0]), []).append({
                "entry": int(row[1] or 0),
                "doc": int(row[2] or 0),
                "vendor": _text(row[3]),
                "eta": _date(row[4]),
                "ordered": _date(row[5]),
                "qty": _num(row[6]),
                "price": _num(row[7]),
            })
        return out

    def measured_lead_times(self, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """PO date -> goods-receipt date, averaged per item over 18 months.

        Measured, not asked for. A lead time somebody typed into a template is an
        intention; this is what the supplier has actually done.
        """
        if not codes:
            return {}
        rows = self._rows_for_codes(f"""
            SELECT G."ItemCode", COUNT(*),
                   AVG(DAYS_BETWEEN(P."DocDate", GH."DocDate")),
                   MAX(DAYS_BETWEEN(P."DocDate", GH."DocDate"))
            FROM "{self.production_schema}"."PDN1" G
            JOIN "{self.production_schema}"."OPDN" GH ON GH."DocEntry" = G."DocEntry"
            JOIN "{self.production_schema}"."OPOR" P ON P."DocEntry" = G."BaseEntry"
            WHERE G."BaseType" = {BASE_TYPE_PURCHASE_ORDER}
              AND G."BaseEntry" IS NOT NULL
              AND GH."CANCELED" = 'N'
              AND GH."DocDate" >= ADD_MONTHS(CURRENT_DATE, -{LEAD_TIME_MONTHS})
              AND G."ItemCode" IN ({{codes}})
            GROUP BY G."ItemCode"
        """, codes)
        return {
            _text(row[0]): {
                "lead_lines": int(row[1] or 0),
                "lead_avg": round(_num(row[2]), 1),
                "lead_max": _num(row[3]),
            }
            for row in rows
        }

    def overdue_purchase_summary(self) -> Dict[str, Any]:
        """The company-wide state of the open PO book.

        The trail counts a PO as supply only when its date is credible. That is a
        strong claim, so the dashboard shows the evidence for it rather than
        asking to be believed.
        """
        rows = self._rows(f"""
            SELECT COUNT(*), COUNT(DISTINCT H."DocEntry"),
                   SUM(IFNULL(L."OpenQty", 0) * IFNULL(L."Price", 0)),
                   SUM(CASE WHEN DAYS_BETWEEN(L."ShipDate", CURRENT_DATE) > 180 THEN 1 ELSE 0 END),
                   MIN(L."ShipDate")
            FROM "{self.production_schema}"."OPOR" H
            JOIN "{self.production_schema}"."POR1" L ON L."DocEntry" = H."DocEntry"
            WHERE H."CANCELED" = 'N' AND L."LineStatus" = 'O' AND L."OpenQty" > 0
              AND L."ShipDate" < CURRENT_DATE
        """)
        row = rows[0] if rows else (0, 0, 0, 0, None)
        return {
            "overdue_po_lines": int(row[0] or 0),
            "overdue_po_docs": int(row[1] or 0),
            "overdue_po_value": _num(row[2]),
            "overdue_po_over180": int(row[3] or 0),
            "overdue_po_oldest": _date(row[4]),
        }

    # ── make instead of buy ───────────────────────────────────────────────────

    def sub_bills_of_material(self, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """A purchased item that carries its own BOM has an in-house route.

        That is the whole make-or-buy question, already answered in SAP and never
        looked at: if we can build the thing from a preform and a conversion
        rate for less than the last price paid, the shortage has a second answer.
        """
        return self.bills_of_material(codes)
