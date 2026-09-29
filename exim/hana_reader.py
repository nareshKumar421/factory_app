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
