"""What Import / Export reads from SAP itself: the raw-material oils an oil
here must be one of, and finished oil on hand.

RAW-MATERIAL OILS are EXIM's own rule for them (its ``sap_sync`` RM query):
items coded RM... whose ``U_Unit`` is OIL. An oil on the tank farm is one of
these, so its code and name are SAP's, never typed.

FINISHED OIL: the director inventory sets the oil in the tanks and on its way
against the oil already packed. EXIM asked SAP for that through a SQL Server linked to HANA
(``OPENQUERY(HANADB112, ...)``) with the schema written into the query; here it
is one parameterised query on the company's own HANA connection.

WHAT IT COUNTS, as EXIM did: finished items (``ItemCode`` FG...) kept in litres
(``U_IsLitre = 'Y'``), their on-hand quantity from the stock ledger (``OINM``,
in minus out, up to today) times the litres in a sales pack (``SalPackUn``), in
the two finished-goods warehouses, leaving out ghee. An item with no customs
chapter is left out too: EXIM's query joined ``OCHP`` without an outer join.
"""

import logging
from decimal import Decimal

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

#: The finished-goods warehouses the director inventory reports.
FINISHED_WAREHOUSES = ("BH-EC", "GP-FG")


def _read(company_code: str, query: str, params: list, what: str) -> list:
    connection = HanaConnection(CompanyContext(company_code).hana)
    try:
        conn = connection.connect()
    except dbapi.Error as exc:
        logger.warning("exim: SAP HANA connection failed: %s", exc)
        raise SAPConnectionError("Unable to connect to SAP HANA.") from exc
    try:
        cursor = conn.cursor()
        cursor.execute(query.format(schema=connection.schema), params)
        return cursor.fetchall()
    except dbapi.Error as exc:
        logger.warning("exim: %s read failed: %s", what, exc)
        raise SAPDataError(f"Failed to read {what} from SAP.") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


_RAW_OILS = """
    SELECT "ItemCode", "ItemName", "U_Sub_Group", "frozenFor"
      FROM "{schema}"."OITM"
     WHERE "ItemCode" LIKE 'RM%' AND "U_Unit" = 'OIL'
"""


def _raw_oil(row) -> dict:
    code, name, sub_group, frozen = row
    return {
        "code": (code or "").strip(),
        "name": (name or "").strip(),
        # SAP's variety (CANOLA, OLIVE ...): the screen offers it as the category.
        "sub_group": (sub_group or "").strip(),
        "frozen": frozen == "Y",
    }


def raw_material_oils(company_code: str) -> list:
    """Every raw-material oil in SAP, by code. Raises SAPConnectionError or
    SAPDataError."""
    rows = _read(company_code, _RAW_OILS + ' ORDER BY "ItemCode"', [], "raw-material oils")
    return [_raw_oil(row) for row in rows]


def raw_material_oil(company_code: str, code: str) -> dict | None:
    """The raw-material oil with this code, or None if SAP has no such oil."""
    rows = _read(company_code, _RAW_OILS + ' AND "ItemCode" = ?', [code], "raw-material oil")
    return _raw_oil(rows[0]) if rows else None


#: SAP's raw-material oils, as a subquery: an oil contract is a PO line for one.
_OIL_ITEMS = """(SELECT "ItemCode" FROM "{schema}"."OITM" WHERE "ItemCode" LIKE 'RM%' AND "U_Unit" = 'OIL')"""

_PO_LINES = """
    SELECT H."DocEntry", H."DocNum", H."DocDate", H."CardCode", H."CardName",
           L."LineNum", L."ItemCode", L."Dscription", L."unitMsr",
           L."Quantity", L."OpenQty", L."Price", L."LineTotal", L."LineStatus", L."WhsCode"
      FROM "{schema}"."OPOR" H
      JOIN "{schema}"."POR1" L ON L."DocEntry" = H."DocEntry"
     WHERE H."CANCELED" = 'N' AND COALESCE(L."Currency", 'INR') = 'INR'
       AND L."ItemCode" IN """ + _OIL_ITEMS


def _po_line(row) -> dict:
    (doc_entry, doc_num, doc_date, card_code, card_name, line_num, item_code, item_name, unit,
     quantity, open_qty, price, line_total, line_status, warehouse) = row
    return {
        "doc_entry": doc_entry,
        "po_number": str(doc_num),
        "po_date": doc_date.date() if hasattr(doc_date, "date") else doc_date,
        "vendor_code": (card_code or "").strip(),
        "vendor_name": (card_name or "").strip(),
        "line": line_num,
        "item_code": (item_code or "").strip(),
        "item_name": (item_name or "").strip(),
        "unit": (unit or "").strip().upper(),
        "quantity": Decimal(str(quantity or 0)),
        "open_qty": Decimal(str(open_qty or 0)),
        "rate": Decimal(str(price or 0)),
        "value": Decimal(str(line_total or 0)),
        "closed": line_status == "C",
        "warehouse": (warehouse or "").strip(),
    }


def oil_po_lines(company_code: str, *, date_from=None, date_to=None, open_only=False, po_number=None) -> list:
    """The raw-material oil lines of SAP's domestic purchase orders, cancelled
    ones left out: by PO date, still open, or one PO. Raises SAPConnectionError
    or SAPDataError.

    Domestic means priced in rupees. An import (a USD or EUR line) is left out:
    its landed cost is its customs duty and clearing too, which neither the PO
    nor EXIM's domestic contract sheet holds, and it is followed as an oil lot."""
    query, params = _PO_LINES, []
    if date_from is not None:
        query += ' AND H."DocDate" >= ?'
        params.append(date_from)
    if date_to is not None:
        query += ' AND H."DocDate" <= ?'
        params.append(date_to)
    if open_only:
        query += """ AND L."LineStatus" = 'O'"""
    if po_number is not None:
        query += ' AND H."DocNum" = ?'
        params.append(int(po_number))
    query += ' ORDER BY H."DocDate" DESC, H."DocNum" DESC, L."LineNum"'
    return [_po_line(row) for row in _read(company_code, query, params, "oil purchase orders")]


_GRPO_LINES = """
    SELECT H."DocNum", H."DocDate", H."NumAtCard", H."U_VehicleNoM", H."U_TransporterName",
           H."U_BilltyNumber", L."BaseEntry", L."BaseLine", L."Quantity", L."LineTotal"
      FROM "{schema}"."OPDN" H
      JOIN "{schema}"."PDN1" L ON L."DocEntry" = H."DocEntry"
     WHERE H."CANCELED" = 'N' AND L."BaseType" = 22 AND L."BaseEntry" IN ({marks})
"""


def oil_grpo_lines(company_code: str, po_doc_entries) -> list:
    """The GRPO lines received against these POs, cancelled GRPOs left out.

    A GRPO line is one truck: its ``Quantity`` is what was weighed in and its
    ``LineTotal`` what the supplier billed (SAP spreads the bill over the weighed
    quantity, so the line's price is the rate after the shortage)."""
    entries = sorted({int(e) for e in po_doc_entries})
    found = []
    for start in range(0, len(entries), 500):
        chunk = entries[start:start + 500]
        query = _GRPO_LINES.replace("{marks}", ", ".join("?" for _ in chunk))
        for row in _read(company_code, query + ' ORDER BY H."DocDate", H."DocNum"', chunk, "oil GRPOs"):
            doc_num, doc_date, invoice, vehicle, transporter, bilty, base_entry, base_line, qty, total = row
            found.append({
                "grpo_number": str(doc_num),
                "grpo_date": doc_date.date() if hasattr(doc_date, "date") else doc_date,
                "invoice_no": (invoice or "").strip(),
                "vehicle_number": (vehicle or "").strip(),
                "transporter": (transporter or "").strip(),
                "bilty_number": (bilty or "").strip(),
                "po_doc_entry": base_entry,
                "po_line": base_line,
                "quantity": Decimal(str(qty or 0)),
                "value": Decimal(str(total or 0)),
            })
    return found


def finished_litres(company_code: str) -> dict:
    """{warehouse: litres} for ``FINISHED_WAREHOUSES``. Raises SAPConnectionError
    or SAPDataError; the caller decides what the page shows then."""
    marks = ", ".join("?" for _ in FINISHED_WAREHOUSES)
    query = f"""
        SELECT T0."Warehouse", SUM((T0."InQty" - T0."OutQty") * T1."SalPackUn") AS litres
          FROM "{{schema}}"."OINM" T0
          JOIN "{{schema}}"."OITM" T1 ON T0."ItemCode" = T1."ItemCode"
          JOIN "{{schema}}"."OCHP" T2 ON T2."AbsEntry" = T1."ChapterID"
         WHERE T0."DocDate" <= CURRENT_DATE
           AND T1."U_IsLitre" = 'Y'
           AND T1."ItemCode" LIKE 'FG%'
           AND T0."Warehouse" IN ({marks})
           AND (CASE WHEN T1."ItemName" LIKE 'GIFT%' THEN 'BLENDED' ELSE T1."U_Sub_Group" END) <> 'GHEE'
         GROUP BY T0."Warehouse"
    """
    rows = _read(company_code, query, list(FINISHED_WAREHOUSES), "finished stock")
    found = {warehouse: Decimal(str(litres or 0)) for warehouse, litres in rows}
    return {warehouse: found.get(warehouse, Decimal("0")) for warehouse in FINISHED_WAREHOUSES}
