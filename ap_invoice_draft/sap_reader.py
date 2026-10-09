"""HANA reads behind A/P invoice drafts: open GRPOs, one GRPO with its PO lines,
and the A/P drafts SAP already holds for a GRPO.

A GRPO is "open" while it has lines not yet copied to an A/P invoice; once
accounts adds the invoice SAP closes it, which is what takes it off the picker.
Service GRPOs (transporters' freight, ``DocType = 'S'``) have their own A/P flow
in dispatch and are left out.
"""

import logging
from decimal import Decimal
from typing import Optional

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

#: How many GRPOs the picker offers per search.
PICKER_LIMIT = 50

_OPEN_GRPOS_SQL = """
SELECT TOP {limit} H."DocEntry", H."DocNum", H."DocDate", H."NumAtCard", H."CardCode",
       H."CardName", H."DocTotal", H."Comments"
  FROM "{schema}"."OPDN" H
 WHERE H."DocStatus" = 'O' AND H."CANCELED" = 'N' AND H."DocType" = 'I'
   {search}
 ORDER BY H."DocDate" DESC, H."DocEntry" DESC
"""

_ONE_GRPO_SQL = """
   AND H."DocEntry" = ?
"""

_SEARCH_SQL = """
   AND (CAST(H."DocNum" AS NVARCHAR(20)) LIKE ?
        OR UPPER(IFNULL(H."NumAtCard", '')) LIKE ?
        OR UPPER(H."CardName") LIKE ?
        OR UPPER(H."CardCode") LIKE ?
        OR UPPER(IFNULL(H."Comments", '')) LIKE ?)
"""

# A/P invoices made from a GRPO: any of their lines copied from it. A cancelled
# invoice and its cancellation both carry CANCELED <> 'N' and count for nothing.
_POSTED_INVOICES_SQL = """
SELECT DISTINCT L."BaseEntry", P."DocEntry", P."DocNum", P."DocDate"
  FROM "{schema}"."OPCH" P
  JOIN "{schema}"."PCH1" L ON L."DocEntry" = P."DocEntry"
 WHERE L."BaseType" = 20 AND L."BaseEntry" IN ({entries}) AND P."CANCELED" = 'N'
 ORDER BY P."DocEntry"
"""

_GRPO_STATES_SQL = """
SELECT H."DocEntry", H."DocStatus", H."CANCELED"
  FROM "{schema}"."OPDN" H
 WHERE H."DocEntry" IN ({entries})
"""

_LINE_WAREHOUSES_SQL = """
SELECT DISTINCT L."DocEntry", L."WhsCode"
  FROM "{schema}"."PDN1" L
 WHERE L."DocEntry" IN ({entries})
"""

_OPEN_DRAFTS_SQL = """
SELECT DISTINCT L."BaseEntry", D."DocEntry", D."DocNum", D."CreateDate"
  FROM "{schema}"."ODRF" D
  JOIN "{schema}"."DRF1" L ON L."DocEntry" = D."DocEntry"
 WHERE D."ObjType" = '18' AND D."DocStatus" = 'O'
   AND L."BaseType" = 20 AND L."BaseEntry" IN ({entries})
 ORDER BY D."DocEntry"
"""

_GRPO_HEADER_SQL = """
SELECT H."DocEntry", H."DocNum", H."DocDate", H."TaxDate", H."CreateDate", H."DocTime",
       H."CardCode", H."CardName", H."NumAtCard", H."DocTotal", H."VatSum",
       H."BPLId", H."Comments", H."DocStatus", H."CANCELED", H."DocType", H."GSTTranTyp"
  FROM "{schema}"."OPDN" H
 WHERE H."DocEntry" = ?
"""

# A/P invoice series are per month *and* per branch, and each branch-month has
# one per GST transaction type: HR_G0926 ("GA", a GST tax invoice) beside
# HR_B0926 ("--", a bill of supply) and HR_D0926 ("GD"). Accounts' own copies of
# a GRPO take the series whose type is the GRPO's. The "CN.." series share the
# GA type and are not used for bills, so they are passed over.
_AP_SERIES_SQL = """
SELECT S."Series", S."SeriesName", DAYS_BETWEEN(P."F_RefDate", P."T_RefDate") AS "Span"
  FROM "{schema}"."OFPR" P
  JOIN "{schema}"."NNM1" S ON S."Indicator" = P."Indicator" AND S."ObjectCode" = '18'
 WHERE ? BETWEEN TO_DATE(P."F_RefDate") AND TO_DATE(P."T_RefDate")
   AND S."BPLId" = ? AND IFNULL(S."DocSubType", '--') = ?
   AND IFNULL(S."Locked", 'N') = 'N' AND S."SeriesName" NOT LIKE 'CN%'
 ORDER BY "Span", S."Series"
"""

# Each GRPO line beside the PO line it was copied from. ``BaseOpnQty`` is what
# was still open on that PO line when the GRPO took it -- the quantity SAP's own
# 110% rule (SBO_SP_TransactionNotification, error 200017) is measured against.
_GRPO_LINES_SQL = """
SELECT L."LineNum", L."ItemCode", L."Dscription", L."Quantity", L."Price", L."LineTotal",
       L."WhsCode", L."TaxCode", L."VatPrcnt", L."VatSum", L."LineStatus",
       L."BaseType", L."BaseEntry", L."BaseLine", L."BaseOpnQty",
       P."DocNum" AS "PoDocNum", PL."Price" AS "PoPrice", PL."Quantity" AS "PoQuantity",
       PL."TaxCode" AS "PoTaxCode", PL."VatPrcnt" AS "PoVatPrcnt"
  FROM "{schema}"."PDN1" L
  LEFT JOIN "{schema}"."POR1" PL
         ON L."BaseType" = 22 AND PL."DocEntry" = L."BaseEntry" AND PL."LineNum" = L."BaseLine"
  LEFT JOIN "{schema}"."OPOR" P ON P."DocEntry" = PL."DocEntry"
 WHERE L."DocEntry" = ?
 ORDER BY L."LineNum"
"""


class GRPOReader:
    def __init__(self, company_code: str):
        self.connection = HanaConnection(CompanyContext(company_code).hana)
        self.schema = self.connection.schema

    def _rows(self, sql: str, params=(), what="SAP"):
        try:
            conn = self.connection.connect()
        except dbapi.Error as exc:
            logger.warning("ap_invoice_draft: SAP HANA connection failed: %s", exc)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from exc
        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.schema), list(params))
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except dbapi.Error as exc:
            logger.warning("ap_invoice_draft: %s read failed: %s", what, exc)
            raise SAPDataError(f"Failed to read {what} from SAP.") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def open_grpos(self, search: str = "", doc_entry: Optional[int] = None) -> list:
        """Open material GRPOs, newest first, matching ``search`` on the GRPO
        number, the bill number, the vendor or the comments (the gate entry);
        or just the one ``doc_entry``, if it is open."""
        search = (search or "").strip().upper()
        params = []
        clause = ""
        if doc_entry is not None:
            clause = _ONE_GRPO_SQL
            params = [int(doc_entry)]
        elif search:
            like = f"%{search}%"
            clause = _SEARCH_SQL
            params = [like, like, like, like, like]
        rows = self._rows(
            _OPEN_GRPOS_SQL.replace("{limit}", str(PICKER_LIMIT)).replace("{search}", clause),
            params,
            what="open GRPOs",
        )
        entries = [int(r["DocEntry"]) for r in rows]
        warehouses = {}
        if entries:
            for line in self._rows(
                _LINE_WAREHOUSES_SQL.replace("{entries}", ",".join("?" * len(entries))),
                entries,
                what="GRPO warehouses",
            ):
                if line["WhsCode"]:
                    warehouses.setdefault(int(line["DocEntry"]), set()).add(_text(line["WhsCode"]))
        drafts = self.open_ap_drafts(entries)
        return [
            {
                "doc_entry": int(r["DocEntry"]),
                "doc_num": str(r["DocNum"]),
                "doc_date": _day(r["DocDate"]),
                "reference": _text(r["NumAtCard"]),
                "vendor_code": _text(r["CardCode"]),
                "vendor_name": _text(r["CardName"]),
                "total": _dec(r["DocTotal"]),
                "comments": _text(r["Comments"]),
                "warehouses": sorted(warehouses.get(int(r["DocEntry"]), ())),
                "sap_draft_entries": drafts.get(int(r["DocEntry"]), []),
            }
            for r in rows
        ]

    def open_ap_drafts(self, grpo_entries: list) -> dict:
        """``{grpo doc-entry: [open A/P draft doc-entries]}`` for the GRPOs given.

        A draft counts while SAP still holds it open; once accounts adds it as
        an invoice the draft closes and the GRPO with it.
        """
        entries = [int(e) for e in grpo_entries]
        if not entries:
            return {}
        found = {}
        for r in self._rows(
            _OPEN_DRAFTS_SQL.replace("{entries}", ",".join("?" * len(entries))),
            entries,
            what="A/P invoice drafts",
        ):
            found.setdefault(int(r["BaseEntry"]), []).append(int(r["DocEntry"]))
        return found

    def ap_invoice_states(self, grpo_entries: list) -> dict:
        """Where each GRPO's A/P invoice stands in SAP.

        ``{grpo doc-entry: {"grpo_open", "grpo_cancelled", "invoices": [...],
        "draft_entries": [...]}}``; a GRPO SAP does not have is left out.
        """
        entries = [int(e) for e in grpo_entries]
        if not entries:
            return {}
        marks = ",".join("?" * len(entries))
        states = {
            int(r["DocEntry"]): {
                "grpo_open": r["DocStatus"] == "O",
                "grpo_cancelled": r["CANCELED"] != "N",
                "invoices": [],
                "draft_entries": [],
            }
            for r in self._rows(_GRPO_STATES_SQL.replace("{entries}", marks), entries, what="GRPOs")
        }
        for r in self._rows(_POSTED_INVOICES_SQL.replace("{entries}", marks), entries, what="A/P invoices"):
            state = states.get(int(r["BaseEntry"]))
            if state is not None:
                state["invoices"].append({
                    "doc_entry": int(r["DocEntry"]),
                    "doc_num": str(r["DocNum"]),
                    "doc_date": _day(r["DocDate"]),
                })
        for grpo_entry, drafts in self.open_ap_drafts(list(states)).items():
            states[grpo_entry]["draft_entries"] = drafts
        return states

    def ap_invoice_series(self, posting_date, branch_id: int, gst_type: str):
        """``(series, name)`` an A/P invoice dated ``posting_date`` takes in
        ``branch_id`` for the GST type, or ``None`` when SAP has none open."""
        rows = self._rows(
            _AP_SERIES_SQL, [posting_date, int(branch_id), gst_type or "--"], what="A/P invoice series",
        )
        return (int(rows[0]["Series"]), _text(rows[0]["SeriesName"])) if rows else None

    def grpo(self, doc_entry: int):
        """One GRPO with every line beside its PO line, or ``None``."""
        header = self._rows(_GRPO_HEADER_SQL, [int(doc_entry)], what="GRPO")
        if not header:
            return None
        h = header[0]
        lines = self._rows(_GRPO_LINES_SQL, [int(doc_entry)], what="GRPO lines")
        return {
            "doc_entry": int(h["DocEntry"]),
            "doc_num": str(h["DocNum"]),
            "doc_date": _day(h["DocDate"]),
            "tax_date": _day(h["TaxDate"]),
            # When the GRPO was actually made: DocDate can be typed backwards.
            "created_on": _day(h["CreateDate"]),
            "reference": _text(h["NumAtCard"]),
            "vendor_code": _text(h["CardCode"]),
            "vendor_name": _text(h["CardName"]),
            "total": _dec(h["DocTotal"]),
            "tax_total": _dec(h["VatSum"]),
            "branch_id": int(h["BPLId"]) if h["BPLId"] is not None else None,
            "comments": _text(h["Comments"]),
            "is_open": h["DocStatus"] == "O",
            "is_cancelled": h["CANCELED"] != "N",
            "is_service": h["DocType"] == "S",
            "gst_type": _text(h["GSTTranTyp"]) or "--",
            "lines": [
                {
                    "line_num": int(r["LineNum"]),
                    "item_code": _text(r["ItemCode"]),
                    "description": _text(r["Dscription"]),
                    "quantity": _dec(r["Quantity"]),
                    "price": _dec(r["Price"]),
                    "line_total": _dec(r["LineTotal"]),
                    "warehouse": _text(r["WhsCode"]),
                    "tax_code": _text(r["TaxCode"]),
                    "tax_rate": _dec(r["VatPrcnt"]),
                    "tax_amount": _dec(r["VatSum"]),
                    "is_open": r["LineStatus"] == "O",
                    "from_po": r["BaseType"] == 22,
                    "po_doc_entry": int(r["BaseEntry"]) if r["BaseType"] == 22 else None,
                    "po_line": int(r["BaseLine"]) if r["BaseType"] == 22 else None,
                    "po_num": str(r["PoDocNum"]) if r["PoDocNum"] is not None else "",
                    "po_open_qty": _dec(r["BaseOpnQty"]) if r["BaseType"] == 22 else None,
                    "po_price": _dec(r["PoPrice"]) if r["PoPrice"] is not None else None,
                    "po_quantity": _dec(r["PoQuantity"]) if r["PoQuantity"] is not None else None,
                    "po_tax_code": _text(r["PoTaxCode"]),
                    "po_tax_rate": _dec(r["PoVatPrcnt"]) if r["PoVatPrcnt"] is not None else None,
                }
                for r in lines
            ],
        }


def _dec(value) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def _day(value):
    return value.date() if hasattr(value, "date") else value


def _text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""
