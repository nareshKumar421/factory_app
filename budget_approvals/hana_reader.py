"""
budget_approvals/hana_reader.py

Reads budget approval draft lines from SAP HANA for the Budget Approvals
Dashboard.

This mirrors the DRAFT_APPROVAL_Budget stored procedure, but does NOT call
it: the procedure takes no parameters, copies both companies' full journal
history (JDT1) into temp tables and runs a correlated subquery per draft
line, which takes minutes — far past any interactive request. Running the
procedure's own SELECT with the budget filter pushed down, and fetching the
per-month posted totals as a separate ~24-row aggregate joined in Python,
returns the identical columns in well under a second per company.

One HANA login sees every company schema, so a single connection serves
both the Oil and Beverages branches.
"""

import logging
from typing import Any, Dict, List

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

# SAP B1 document object types that appear on approval drafts.
OBJ_TYPE_LABELS = {
    "13": "A/R Invoice",
    "14": "A/R Credit Memo",
    "15": "Delivery",
    "16": "Return",
    "17": "Sales Order",
    "18": "A/P Invoice",
    "19": "A/P Credit Memo",
    "20": "Goods Receipt PO",
    "21": "Goods Return",
    "22": "Purchase Order",
    "59": "Goods Receipt",
    "60": "Goods Issue",
    "67": "Inventory Transfer",
    "1250000001": "Inventory Transfer Request",
}


class HanaBudgetApprovalReader:
    """
    Reads draft approval lines for one budget head across company schemas.

    Args:
        context:        CompanyContext supplying the HANA credentials.
        branch_schemas: {branch label: HANA schema} to read, e.g.
                        {"OIL": "JIVO_OIL_HANADB", "BEVERAGE": ...}.
    """

    def __init__(self, context, branch_schemas: Dict[str, str]):
        self.connection = HanaConnection(context.hana)
        self.schema = self.connection.schema
        self.branch_schemas = branch_schemas

    # ------------------------------------------------------------------
    # Public Methods
    # ------------------------------------------------------------------

    def get_draft_approval_rows(self, budget: str) -> List[Dict[str, Any]]:
        """
        Returns every draft approval line whose budget dimension (OcrCode3)
        matches `budget` (case-insensitive), across all configured branches,
        as dicts keyed by API field names.
        """
        budget_upper = budget.strip().upper()

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            try:
                rows: List[Dict[str, Any]] = []
                for label, schema in self.branch_schemas.items():
                    posted = self._fetch_posted_by_month(cursor, schema, budget_upper)
                    rows.extend(
                        self._fetch_branch_rows(
                            cursor, label, schema, budget_upper, posted
                        )
                    )
                return rows
            finally:
                cursor.close()

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error in budget approvals: {e}")
            raise SAPDataError(
                "Failed to retrieve budget approval data from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error in budget approvals: {e}")
            raise SAPDataError(
                "Failed to retrieve budget approval data from SAP. Please try again."
            ) from e
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _fetch_posted_by_month(
        self, cursor, schema: str, budget_upper: str
    ) -> Dict[str, float]:
        """
        Posted journal expense per MM-YYYY month for the budget head — the
        procedure's #EXPFORTHEMONTH temp-table aggregation, pre-filtered.
        Joined to the draft lines in Python: HANA picks a catastrophic plan
        when this aggregate is joined in SQL (53s vs 0.3s measured).
        """
        cursor.execute(
            f'''
SELECT TO_CHAR(B."RefDate",'MM-YYYY') AS "MONTH",
       SUM(A."Debit" - A."Credit") AS "AMOUNT"
FROM "{schema}"."JDT1" A
INNER JOIN "{schema}"."OJDT" B ON A."TransId" = B."TransId"
INNER JOIN "{schema}"."OACT" C ON C."AcctCode" = A."Account"
INNER JOIN "{schema}"."OACT" D ON C."FatherNum" = D."AcctCode"
WHERE UPPER(A."OcrCode3") = ?
GROUP BY TO_CHAR(B."RefDate",'MM-YYYY')
''',
            [budget_upper],
        )
        return {row[0]: float(row[1] or 0) for row in cursor.fetchall()}

    def _fetch_branch_rows(
        self,
        cursor,
        branch_label: str,
        schema: str,
        budget_upper: str,
        posted_by_month: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        cursor.execute(
            f'''
SELECT DISTINCT
  ? AS "Branch",
  RF."DocEntry",
  RF."ObjType",
  F1."LineNum",
  F1."AcctCode",
  OA."AcctName",
  RF."CardCode",
  RF."CardName",
  F1."OcrCode2" AS "EFFECTMONTH",
  F1."OcrCode3" AS "BUDGET",
  F1."OcrCode4" AS "SUB_BUDGET",
  F1."OcrCode5" AS "STATE",
  RF."DocDate",
  F1."LineTotal" AS "AMOUNT",
  TO_CHAR(RF."DocDate",'MM-YYYY') AS "CURRENTMONTH",
  WD."Status",
  OU."U_NAME",
  AU."U_NAME" AS "ApproverName",
  OWD."CreateDate" AS "CreatedDate",
  OWD."CreateTime",
  F1."U_Remarks" AS "LineRemarks",
  RF."Comments",
  OWD."ProcesStat",
  RF."UpdateDate",
  F1."OcrCode"
FROM "{schema}"."ODRF" RF
JOIN "{schema}"."DRF1" F1
  ON RF."DocEntry" = F1."DocEntry" AND RF."ObjType" = F1."ObjType"
LEFT JOIN "{schema}"."OACT" OA ON OA."AcctCode" = F1."AcctCode"
JOIN "{schema}"."OWDD" OWD ON OWD."DraftEntry" = RF."DocEntry"
JOIN "{schema}"."WDD1" WD ON WD."WddCode" = OWD."WddCode"
JOIN "{schema}"."OUSR" OU ON OU."USERID" = OWD."OwnerID"
LEFT JOIN "{schema}"."OUSR" AU ON AU."USERID" = WD."UserID"
WHERE UPPER(F1."OcrCode3") = ?
''',
            [branch_label, budget_upper],
        )
        columns = [description[0] for description in cursor.description or []]
        index = {name: position for position, name in enumerate(columns)}
        rows = [self._map_row(row, index, posted_by_month) for row in cursor.fetchall()]
        return self._merge_approver_rows(rows)

    # ------------------------------------------------------------------
    # Row Mapper
    # ------------------------------------------------------------------

    def _map_row(
        self, row, index: Dict[str, int], posted_by_month: Dict[str, float]
    ) -> Dict[str, Any]:
        def value(column: str):
            position = index.get(column)
            return row[position] if position is not None else None

        obj_type = self._text(value("ObjType"))
        current_month = self._text(value("CURRENTMONTH"))

        return {
            "branch": self._text(value("Branch")),
            "doc_entry": int(value("DocEntry") or 0),
            "obj_type": obj_type,
            "obj_type_label": OBJ_TYPE_LABELS.get(obj_type, obj_type),
            "line_num": self._int_or_none(value("LineNum")),
            "acct_code": self._text(value("AcctCode")),
            "acct_name": self._text(value("AcctName")),
            "card_code": self._text(value("CardCode")),
            "card_name": self._text(value("CardName")),
            "effect_month": self._text(value("EFFECTMONTH")),
            "budget": self._text(value("BUDGET")),
            "sub_budget": self._text(value("SUB_BUDGET")),
            "state": self._text(value("STATE")),
            "doc_date": self._date(value("DocDate")),
            "amount": float(value("AMOUNT") or 0),
            "current_month": current_month,
            "current_month_posted_amount": posted_by_month.get(current_month, 0.0),
            "status": self._text(value("Status")),
            "owner": self._text(value("U_NAME")),
            "approver": self._text(value("ApproverName")),
            "created_date": self._date(value("CreatedDate")),
            "created_time": self._time(value("CreateTime")),
            "line_remarks": self._text(value("LineRemarks")),
            "comments": self._text(value("Comments")),
            "process_status": self._text(value("ProcesStat")),
            "update_date": self._date(value("UpdateDate")),
            "ocr_code": self._text(value("OcrCode")),
        }

    @staticmethod
    def _merge_approver_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        A request stage can have several approvers with the same decision;
        before the approver column existed those rows collapsed under SELECT
        DISTINCT. Collapse them again here — same line, same status, several
        approvers — joining the names, so line counts and amount totals stay
        exactly what the procedure produced.
        """
        merged: Dict[tuple, Dict[str, Any]] = {}
        for row in rows:
            key = tuple(
                (field, val) for field, val in row.items() if field != "approver"
            )
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(row)
            elif row["approver"] and row["approver"] not in existing["approver"]:
                existing["approver"] = ", ".join(
                    name for name in (existing["approver"], row["approver"]) if name
                )
        return list(merged.values())

    @staticmethod
    def _text(raw) -> str:
        if raw is None:
            return ""
        return str(raw).strip()

    @staticmethod
    def _int_or_none(raw):
        return int(raw) if raw is not None else None

    @staticmethod
    def _date(raw):
        if raw is None:
            return None
        return raw.strftime("%Y-%m-%d")

    @staticmethod
    def _time(raw):
        """SAP stores times as integer HHMM (e.g. 933 -> 09:33)."""
        if raw is None:
            return ""
        try:
            numeric = int(raw)
        except (TypeError, ValueError):
            return str(raw)
        return f"{numeric // 100:02d}:{numeric % 100:02d}"
