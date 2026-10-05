"""HANA reads behind the outstanding reports: who owes what, and what is open.

Ported from EXIM's finance screens (``sap_sync`` Open A/P, Open A/R, Open
GRPOs, the Dr/Cr and vendor/customer outstanding sheets and customer aging),
which read SAP through a SQL Server linked to HANA with the company written
into each query - Oil for every vendor report, Beverages for every customer
one, with nothing on the page saying so. Here every read follows the request's
company, and the corrections EXIM needed are made in the SQL:

 - Open GRPOs are counted per GRPO, not once per line.
 - Customer aging takes only what is still open (EXIM aged every invoice since
   2024, paid or not), nets open credit notes, and ages by the due date.
 - Open A/P keeps every open invoice; EXIM hid those before 1 April unless the
   date box was emptied by hand.
 - EXIM's hand-typed list of 143 "oil" parties is replaced by a filter that
   finds them: a vendor who has a purchase order for a raw-material oil.

Custom fields (a bill's bilty, transporter, vehicle) are SAP user fields, which
each company's database may or may not carry, so they are selected only where
the schema has them.
"""

import logging
from decimal import Decimal

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

#: SAP's raw-material oils (EXIM's own rule), for the oil-suppliers filter.
OIL_SUPPLIERS = """(
    SELECT DISTINCT P."CardCode" FROM "{schema}"."OPOR" P
      JOIN "{schema}"."POR1" PL ON PL."DocEntry" = P."DocEntry"
      JOIN "{schema}"."OITM" I ON I."ItemCode" = PL."ItemCode"
     WHERE P."CANCELED" = 'N' AND I."ItemCode" LIKE 'RM%' AND I."U_Unit" = 'OIL'
)"""

#: Transport and receipt fields on a bill, when the company's schema has them.
TRANSPORT_FIELDS = {
    "U_BilltyNumber": "bilty_number",
    "U_BiltyDate": "bilty_date",
    "U_TransporterName": "transporter",
    "U_VehicleNoM": "vehicle_number",
    "U_LRNUmber": "lr_number",
    "U_Recv_Date": "received_date",
}


class _Reader:
    def __init__(self, company_code: str):
        self.connection = HanaConnection(CompanyContext(company_code).hana)
        self.schema = self.connection.schema

    def rows(self, sql: str, params=(), what="SAP"):
        try:
            conn = self.connection.connect()
        except dbapi.Error as exc:
            logger.warning("outstanding: SAP HANA connection failed: %s", exc)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from exc
        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.schema), list(params))
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except dbapi.Error as exc:
            logger.warning("outstanding: %s read failed: %s", what, exc)
            raise SAPDataError(f"Failed to read {what} from SAP.") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def user_fields(self, table: str) -> list:
        """The transport fields this company's ``table`` has, as (column, key)."""
        return [(col, key) for col, key in TRANSPORT_FIELDS.items()
                if col in _columns(self.schema, table, self)]


def _columns(schema: str, table: str, reader: _Reader) -> frozenset:
    key = (schema, table)
    if key not in _COLUMN_CACHE:
        rows = reader.rows(
            'SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?',
            (schema, table), what="table columns",
        )
        _COLUMN_CACHE[key] = frozenset(r["COLUMN_NAME"] for r in rows)
    return _COLUMN_CACHE[key]


_COLUMN_CACHE: dict = {}


def _dec(value) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def _day(value):
    return value.date() if hasattr(value, "date") else value


def _text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _person(value) -> str:
    """A sales employee or buyer; SAP's "-No Sales Employee / Buyer-" is nobody."""
    name = _text(value)
    return "" if name.startswith("-No Sales Employee") else name


# ---------------------------------------------------------------------------
# Party balances
# ---------------------------------------------------------------------------

_PARTY_SQL = """
WITH CARDS AS (
    SELECT C."CardCode", C."CardName", C."CardType", C."Balance", C."Currency", C."GroupCode", C."SlpCode"
      FROM "{schema}"."OCRD" C
     WHERE C."CardType" = ? AND C."Balance" <> 0 {oil}
),
BILLS AS (
    SELECT B."CardCode", B."DocNum", B."DocDate", B."DocTotal",
           ROW_NUMBER() OVER (PARTITION BY B."CardCode" ORDER BY B."DocDate" DESC, B."DocEntry" DESC) AS RN
      FROM "{schema}"."{bill}" B
     WHERE B."CANCELED" = 'N' AND B."CardCode" IN (SELECT "CardCode" FROM CARDS)
),
PAYS AS (
    SELECT P."CardCode", P."DocNum", P."DocDate", P."DocTotal",
           ROW_NUMBER() OVER (PARTITION BY P."CardCode" ORDER BY P."DocDate" DESC, P."DocEntry" DESC) AS RN
      FROM "{schema}"."{pay}" P
     WHERE P."Canceled" = 'N' AND P."CardCode" IN (SELECT "CardCode" FROM CARDS)
)
SELECT C."CardCode", C."CardName", C."Balance", C."Currency", G."GroupName", S."SlpName",
       B."DocNum" AS "BillNum", B."DocDate" AS "BillDate", B."DocTotal" AS "BillTotal",
       P."DocNum" AS "PayNum", P."DocDate" AS "PayDate", P."DocTotal" AS "PayTotal"
  FROM CARDS C
  LEFT JOIN "{schema}"."OCRG" G ON G."GroupCode" = C."GroupCode"
  LEFT JOIN "{schema}"."OSLP" S ON S."SlpCode" = C."SlpCode"
  LEFT JOIN BILLS B ON B."CardCode" = C."CardCode" AND B.RN = 1
  LEFT JOIN PAYS P ON P."CardCode" = C."CardCode" AND P.RN = 1
 ORDER BY ABS(C."Balance") DESC
"""


def party_balances(company_code: str, side: str, *, oil_suppliers: bool = False) -> list:
    """Every vendor (``side`` "vendor") or customer with a balance: what SAP
    holds against them (OCRD.Balance, positive = they owe us), their last bill
    and their last payment. Raises SAPConnectionError / SAPDataError."""
    vendor = side == "vendor"
    sql = _PARTY_SQL.format(
        schema="{schema}",
        bill="OPCH" if vendor else "OINV",
        pay="OVPM" if vendor else "ORCT",
        oil=f'AND C."CardCode" IN {OIL_SUPPLIERS}' if vendor and oil_suppliers else "",
    )
    rows = _Reader(company_code).rows(sql, ("S" if vendor else "C",), what="party balances")
    return [{
        "card_code": _text(r["CardCode"]),
        "card_name": _text(r["CardName"]),
        "group": _text(r["GroupName"]),
        "sales_employee": _person(r["SlpName"]),
        "balance": _dec(r["Balance"]),
        "currency": _text(r["Currency"]),
        "last_bill": {"number": str(r["BillNum"]), "date": _day(r["BillDate"]), "total": _dec(r["BillTotal"])}
        if r["BillNum"] is not None else None,
        "last_payment": {"number": str(r["PayNum"]), "date": _day(r["PayDate"]), "total": _dec(r["PayTotal"])}
        if r["PayNum"] is not None else None,
    } for r in rows]


# ---------------------------------------------------------------------------
# Open bills
# ---------------------------------------------------------------------------

_OPEN_BILLS_SQL = """
SELECT H."DocEntry", H."DocNum", H."DocDate", H."DocDueDate", H."NumAtCard", H."CardCode", H."CardName",
       H."DocCur", H."DocTotal", H."PaidToDate", H."DocTotalFC", H."PaidFC", H."Comments",
       G."GroupName", S."SlpName" {extra}
  FROM "{schema}"."{table}" H
  JOIN "{schema}"."OCRD" C ON C."CardCode" = H."CardCode"
  LEFT JOIN "{schema}"."OCRG" G ON G."GroupCode" = C."GroupCode"
  LEFT JOIN "{schema}"."OSLP" S ON S."SlpCode" = H."SlpCode"
 WHERE H."DocStatus" = 'O' AND H."CANCELED" = 'N' {oil}
 ORDER BY H."DocDueDate", H."DocNum"
"""


def open_bills(company_code: str, side: str, *, oil_suppliers: bool = False) -> list:
    """Every open A/P invoice (``side`` "vendor") or A/R invoice ("customer"),
    with what is still due on it (total less paid). Raises SAP errors."""
    vendor = side == "vendor"
    table = "OPCH" if vendor else "OINV"
    reader = _Reader(company_code)
    fields = reader.user_fields(table)
    extra = "".join(f', H."{col}"' for col, _ in fields)
    sql = _OPEN_BILLS_SQL.format(
        schema="{schema}", table=table, extra=extra,
        oil=f'AND H."CardCode" IN {OIL_SUPPLIERS}' if vendor and oil_suppliers else "",
    )
    out = []
    for r in reader.rows(sql, what="open invoices"):
        total, paid = _dec(r["DocTotal"]), _dec(r["PaidToDate"])
        out.append({
            "doc_entry": r["DocEntry"],
            "doc_num": str(r["DocNum"]),
            "doc_date": _day(r["DocDate"]),
            "due_date": _day(r["DocDueDate"]),
            "party_ref": _text(r["NumAtCard"]),
            "card_code": _text(r["CardCode"]),
            "card_name": _text(r["CardName"]),
            "group": _text(r["GroupName"]),
            "sales_employee": _person(r["SlpName"]),
            "currency": _text(r["DocCur"]),
            "total": total,
            "paid": paid,
            "due": total - paid,
            "total_fc": _dec(r["DocTotalFC"]),
            "paid_fc": _dec(r["PaidFC"]),
            "remarks": _text(r["Comments"]),
            **{key: (_day(r[col]) if key.endswith("_date") else _text(r[col])) for col, key in fields},
        })
    return out


# ---------------------------------------------------------------------------
# Open GRPOs: goods received, not yet billed
# ---------------------------------------------------------------------------

_OPEN_GRPOS_SQL = """
SELECT H."DocEntry", H."DocNum", H."DocDate", H."NumAtCard", H."CardCode", H."CardName", H."DocTotal",
       H."DocCur", U."U_NAME" {extra}
  FROM "{schema}"."OPDN" H
  LEFT JOIN "{schema}"."OUSR" U ON U."USERID" = H."UserSign"
 WHERE H."DocStatus" = 'O' AND H."CANCELED" = 'N'
 ORDER BY H."DocDate", H."DocNum"
"""

_OPEN_GRPO_LINES_SQL = """
SELECT L."DocEntry", L."WhsCode", L."ItemCode"
  FROM "{schema}"."PDN1" L
  JOIN "{schema}"."OPDN" H ON H."DocEntry" = L."DocEntry"
 WHERE H."DocStatus" = 'O' AND H."CANCELED" = 'N'
"""


def open_grpos(company_code: str) -> list:
    """Every goods receipt PO not yet billed, one row per GRPO with the
    warehouses it went into (EXIM listed each line as a GRPO of its own).
    Raises SAP errors."""
    reader = _Reader(company_code)
    fields = reader.user_fields("OPDN")
    extra = "".join(f', H."{col}"' for col, _ in fields)
    lines = {}
    for line in reader.rows(_OPEN_GRPO_LINES_SQL, what="open GRPO lines"):
        entry = lines.setdefault(line["DocEntry"], {"warehouses": set(), "count": 0, "rm": False})
        entry["count"] += 1
        if line["WhsCode"]:
            entry["warehouses"].add(_text(line["WhsCode"]))
        entry["rm"] = entry["rm"] or _text(line["ItemCode"]).startswith("RM")
    out = []
    for r in reader.rows(_OPEN_GRPOS_SQL.format(schema="{schema}", extra=extra), what="open GRPOs"):
        info = lines.get(r["DocEntry"], {"warehouses": set(), "count": 0, "rm": False})
        out.append({
            "doc_entry": r["DocEntry"],
            "doc_num": str(r["DocNum"]),
            "doc_date": _day(r["DocDate"]),
            "party_ref": _text(r["NumAtCard"]),
            "card_code": _text(r["CardCode"]),
            "card_name": _text(r["CardName"]),
            "total": _dec(r["DocTotal"]),
            "currency": _text(r["DocCur"]),
            "user": _text(r["U_NAME"]),
            "warehouses": sorted(info["warehouses"]),
            "lines": info["count"],
            "raw_material": info["rm"],
            **{key: (_day(r[col]) if key.endswith("_date") else _text(r[col])) for col, key in fields},
        })
    return out


# ---------------------------------------------------------------------------
# Customer aging
# ---------------------------------------------------------------------------

_AGING_SQL = """
SELECT 'INVOICE' AS "Kind", H."DocNum", H."DocDate", H."DocDueDate", H."CardCode", H."CardName",
       H."DocTotal" - H."PaidToDate" AS "Due", S."SlpName", G."GroupName"
  FROM "{schema}"."OINV" H
  JOIN "{schema}"."OCRD" C ON C."CardCode" = H."CardCode"
  LEFT JOIN "{schema}"."OCRG" G ON G."GroupCode" = C."GroupCode"
  LEFT JOIN "{schema}"."OSLP" S ON S."SlpCode" = H."SlpCode"
 WHERE H."DocStatus" = 'O' AND H."CANCELED" = 'N'
UNION ALL
SELECT 'CREDIT_NOTE', H."DocNum", H."DocDate", H."DocDueDate", H."CardCode", H."CardName",
       -(H."DocTotal" - H."PaidToDate"), S."SlpName", G."GroupName"
  FROM "{schema}"."ORIN" H
  JOIN "{schema}"."OCRD" C ON C."CardCode" = H."CardCode"
  LEFT JOIN "{schema}"."OCRG" G ON G."GroupCode" = C."GroupCode"
  LEFT JOIN "{schema}"."OSLP" S ON S."SlpCode" = H."SlpCode"
 WHERE H."DocStatus" = 'O' AND H."CANCELED" = 'N'
"""


def open_receivables(company_code: str) -> list:
    """Every open A/R invoice (positive) and open credit note (negative), with
    what is still due on it. Raises SAP errors."""
    return [{
        "kind": r["Kind"],
        "doc_num": str(r["DocNum"]),
        "doc_date": _day(r["DocDate"]),
        "due_date": _day(r["DocDueDate"]),
        "card_code": _text(r["CardCode"]),
        "card_name": _text(r["CardName"]),
        "sales_employee": _person(r["SlpName"]),
        "group": _text(r["GroupName"]),
        "due": _dec(r["Due"]),
    } for r in _Reader(company_code).rows(_AGING_SQL, what="open receivables")]
