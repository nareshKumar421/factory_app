"""HANA reads behind the finance screens ported from SAP Portal.

Journal entries (``OJDT`` + ``JDT1``), the chart of accounts (``OACT``) and the
general ledger of one account, as SAP Portal's ``/api/sap/journal-entries``,
``/chart-of-accounts`` and ``/gl-ledger`` read them (``backend_v1/routes/sap.js``).

One correction to the portal: its ledger anchored the running balance on the
account's live balance (``OACT.CurrTotal``) and walked backwards from the newest
row returned. That is right only when the range runs to today. With an earlier
"to" date every posting after it was missing from the walk, so every balance
shown was off by their sum. The anchor here is the live balance less the
postings after the range.
"""

import logging
from decimal import Decimal

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)

# SAP files every G/L account in one of ten drawers (OACT.GroupMask).
COA_DRAWERS = [
    (1, "Assets"),
    (2, "Liabilities"),
    (3, "Equity"),
    (4, "Revenues"),
    (5, "Cost of Sales"),
    (6, "Expenses"),
    (7, "Financing"),
    (8, "Other Revenues and Gains"),
    (9, "Other Expenses and Losses"),
    (10, "Taxation"),
]
COA_ACCOUNT_TYPES = {"N": "Balance Sheet", "E": "Expenditure", "I": "Income", "O": "Other", "T": "Turnover"}

# OJDT.TransType values an accountant meets most often; others show as the code.
TRANS_TYPE_LABELS = {
    "13": "A/R Invoice",
    "14": "A/R Credit Memo",
    "15": "Delivery",
    "16": "Return",
    "18": "A/P Invoice",
    "19": "A/P Credit Memo",
    "20": "Goods Receipt PO",
    "21": "Goods Return",
    "24": "Incoming Payment",
    "30": "Journal Entry",
    "46": "Outgoing Payment",
    "59": "Goods Receipt",
    "60": "Goods Issue",
    "67": "Inventory Transfer",
    "69": "Landed Costs",
    "162": "Inventory Revaluation",
    "202": "Production Order",
    "-2": "Opening Balance",
}


def _clean(value) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value)


def _money(value) -> float:
    if value is None:
        return 0.0
    return round(float(value if not isinstance(value, Decimal) else value), 2)


def _date(value) -> str | None:
    return value.strftime("%Y-%m-%d") if value is not None and hasattr(value, "strftime") else (value or None)


def _int(value):
    return int(value) if value is not None else None


def _limit(value, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


class HanaFinanceReader:
    """Journal entries, chart of accounts and account ledgers for one company."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Journal entries
    # ------------------------------------------------------------------

    def journal_entries(
        self,
        *,
        trans_id=None,
        number=None,
        reference: str = "",
        trans_type: str = "",
        date_from=None,
        date_to=None,
        limit: int = 20,
    ) -> list[dict]:
        """Newest journal entries matching the filters, each with its lines."""
        where, params = [], []
        if trans_id is not None:
            where.append('H."TransId" = ?')
            params.append(int(trans_id))
        if number is not None:
            where.append('H."Number" = ?')
            params.append(int(number))
        if reference:
            like = f"%{reference.strip().upper()}%"
            where.append(
                """(UPPER(COALESCE(H."BaseRef", '')) LIKE ? OR UPPER(COALESCE(H."Ref1", '')) LIKE ?
                   OR UPPER(COALESCE(H."Ref2", '')) LIKE ? OR UPPER(COALESCE(H."Ref3", '')) LIKE ?)"""
            )
            params.extend([like] * 4)
        if trans_type:
            where.append('CAST(H."TransType" AS NVARCHAR(20)) = ?')
            params.append(str(trans_type).strip())
        if date_from:
            where.append('H."RefDate" >= ?')
            params.append(date_from)
        if date_to:
            where.append('H."RefDate" <= ?')
            params.append(date_to)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        headers = self._query(
            f"""
            SELECT TOP {_limit(limit, 20, 100)}
                H."TransId", H."Number", H."RefDate", H."DueDate", H."TaxDate",
                H."Memo", H."BaseRef", H."TransType",
                CAST(COALESCE(SUM(L."Debit"), 0) AS DECIMAL(19, 2)),
                CAST(COALESCE(SUM(L."Credit"), 0) AS DECIMAL(19, 2))
            FROM "{{schema}}"."OJDT" H
            LEFT JOIN "{{schema}}"."JDT1" L ON L."TransId" = H."TransId"
            {where_sql}
            GROUP BY H."TransId", H."Number", H."RefDate", H."DueDate", H."TaxDate",
                     H."Memo", H."BaseRef", H."TransType"
            ORDER BY H."TransId" DESC
            """,
            tuple(params),
        )
        if not headers:
            return []

        trans_ids = [int(row[0]) for row in headers]
        placeholders = ", ".join("?" for _ in trans_ids)
        lines_by_id: dict[int, list] = {}
        for row in self._query(
            f"""
            SELECT
                L."TransId", L."Line_ID", L."Account", A."AcctName", L."ShortName",
                CAST(COALESCE(L."Debit", 0) AS DECIMAL(19, 2)),
                CAST(COALESCE(L."Credit", 0) AS DECIMAL(19, 2)),
                L."ContraAct", L."LineMemo", L."Project",
                L."ProfitCode", L."OcrCode2", L."OcrCode3", L."OcrCode4", L."OcrCode5"
            FROM "{{schema}}"."JDT1" L
            LEFT JOIN "{{schema}}"."OACT" A ON A."AcctCode" = L."Account"
            WHERE L."TransId" IN ({placeholders})
            ORDER BY L."TransId" DESC, L."Line_ID" ASC
            """,
            tuple(trans_ids),
        ):
            (
                tid, line_id, account, account_name, short_name, debit, credit,
                contra, memo, project, cc1, cc2, cc3, cc4, cc5,
            ) = row
            lines_by_id.setdefault(int(tid), []).append(
                {
                    "line_id": _int(line_id),
                    "account": _clean(account),
                    "account_name": _clean(account_name),
                    "short_name": _clean(short_name),
                    "debit": _money(debit),
                    "credit": _money(credit),
                    "contra_account": _clean(contra),
                    "line_memo": _clean(memo),
                    "project": _clean(project),
                    "cost_centers": [_clean(cc) for cc in (cc1, cc2, cc3, cc4, cc5)],
                }
            )

        entries = []
        for tid, number_, ref_date, due_date, tax_date, memo, base_ref, trans_type_, debit, credit in headers:
            kind = _clean(trans_type_)
            entries.append(
                {
                    "trans_id": int(tid),
                    "number": _int(number_),
                    "ref_date": _date(ref_date),
                    "due_date": _date(due_date),
                    "tax_date": _date(tax_date),
                    "memo": _clean(memo),
                    "base_ref": _clean(base_ref),
                    "trans_type": kind,
                    "trans_type_label": TRANS_TYPE_LABELS.get(kind, kind),
                    "total_debit": _money(debit),
                    "total_credit": _money(credit),
                    "lines": lines_by_id.get(int(tid), []),
                }
            )
        return entries

    # ------------------------------------------------------------------
    # Chart of accounts
    # ------------------------------------------------------------------

    def chart_of_accounts(self, search: str = "", drawer=None) -> dict:
        """The OACT tree with title accounts rolled up, as SAP's window shows it.

        Title accounts hold no balance of their own; their figure is the sum of
        every postable account beneath them. A search keeps every ancestor of a
        hit so the tree still renders a path to it.
        """
        rows = self._query(
            """
            SELECT "AcctCode", "AcctName", "FatherNum", "GroupMask", "Levels", "Postable",
                   "ActType", "ActCurr", "CurrTotal", "FrozenFor", "Details"
            FROM "{schema}"."OACT"
            ORDER BY "AcctCode"
            """,
            (),
        )
        accounts = []
        for code, name, parent, group_mask, level, postable, act_type, currency, balance, frozen, details in rows:
            code = _clean(code)
            if not code:
                continue
            kind = _clean(act_type)
            accounts.append(
                {
                    "code": code,
                    "name": _clean(name),
                    "parent": _clean(parent) or None,
                    "drawer": int(group_mask or 0),
                    "level": int(level or 0),
                    "postable": _clean(postable) == "Y",
                    "type": kind,
                    "type_label": COA_ACCOUNT_TYPES.get(kind, ""),
                    "currency": _clean(currency),
                    "balance": _money(balance),
                    "frozen": _clean(frozen) == "Y",
                    "details": _clean(details),
                }
            )

        by_code = {a["code"]: a for a in accounts}
        for account in accounts:
            account["rollup"] = account["balance"] if account["postable"] else 0.0
        for account in accounts:
            if not account["postable"] or not account["balance"]:
                continue
            seen = {account["code"]}
            parent = account["parent"]
            # A visited set, so a corrupt parent link cannot loop forever.
            while parent and parent in by_code and parent not in seen:
                seen.add(parent)
                node = by_code[parent]
                node["rollup"] = node["rollup"] + account["balance"]
                parent = node["parent"]
        children: dict[str, int] = {}
        for account in accounts:
            account["rollup"] = round(account["rollup"], 2)
            if account["parent"]:
                children[account["parent"]] = children.get(account["parent"], 0) + 1
        for account in accounts:
            account["children"] = children.get(account["code"], 0)

        drawers = [
            {
                "id": drawer_id,
                "label": label,
                "count": sum(1 for a in accounts if a["drawer"] == drawer_id),
                "postable": sum(1 for a in accounts if a["drawer"] == drawer_id and a["postable"]),
                "total": round(
                    sum(a["balance"] for a in accounts if a["drawer"] == drawer_id and a["postable"]), 2
                ),
            }
            for drawer_id, label in COA_DRAWERS
        ]

        data = accounts
        try:
            drawer = int(drawer) if drawer not in (None, "") else None
        except (TypeError, ValueError):
            raise SAPValidationError("drawer must be a number from 1 to 10.")
        if drawer:
            data = [a for a in data if a["drawer"] == drawer]
        needle = (search or "").strip().upper()
        if needle:
            hits = {a["code"] for a in data if needle in a["code"].upper() or needle in a["name"].upper()}
            keep = set(hits)
            for code in hits:
                seen = {code}
                parent = by_code[code]["parent"]
                while parent and parent in by_code and parent not in seen:
                    seen.add(parent)
                    keep.add(parent)
                    parent = by_code[parent]["parent"]
            data = [dict(a, match=a["code"] in hits) for a in data if a["code"] in keep]

        return {
            "accounts": data,
            "drawers": drawers,
            "total": len(accounts),
            "postable": sum(1 for a in accounts if a["postable"]),
        }

    # ------------------------------------------------------------------
    # General ledger of one account
    # ------------------------------------------------------------------

    def general_ledger(self, account: str, date_from=None, date_to=None, limit: int = 200) -> dict:
        """Every posting to one G/L account — or, failing that, one business partner.

        Matches ``JDT1.Account`` or ``JDT1.ShortName`` (SAP's General Ledger lists
        both). The offsetting account can be a G/L code or a partner code, so its
        name comes from OACT first and OCRD second, as SAP's report prints it.
        """
        code = (account or "").strip()
        if not code:
            raise SAPValidationError("account is required.")

        kind = "G/L"
        found = self._query(
            'SELECT "AcctName", "CurrTotal" FROM "{schema}"."OACT" WHERE "AcctCode" = ?', (code,)
        )
        if not found:
            kind = "BP"
            found = self._query(
                'SELECT "CardName", "Balance" FROM "{schema}"."OCRD" WHERE "CardCode" = ?', (code,)
            )
        if not found:
            raise SAPValidationError(f"{code} is neither a G/L account nor a business partner in SAP.")
        name, current_balance = found[0]
        current_balance = _money(current_balance)

        where = ['(J."Account" = ? OR J."ShortName" = ?)']
        params: list = [code, code]
        if date_from:
            where.append('J."RefDate" >= ?')
            params.append(date_from)
        if date_to:
            where.append('J."RefDate" <= ?')
            params.append(date_to)
        where_sql = " AND ".join(where)

        total = self._query(
            f'SELECT COUNT(*) FROM "{{schema}}"."JDT1" J WHERE {where_sql}', tuple(params)
        )
        total = int(total[0][0]) if total else 0

        # The balance after the newest row shown: today's balance less whatever
        # was posted after the range.
        anchor = current_balance
        if date_to:
            later = self._query(
                """
                SELECT COALESCE(SUM(J."Debit" - J."Credit"), 0)
                FROM "{schema}"."JDT1" J
                WHERE (J."Account" = ? OR J."ShortName" = ?) AND J."RefDate" > ?
                """,
                (code, code, date_to),
            )
            anchor = round(current_balance - _money(later[0][0] if later else 0), 2)

        rows = self._query(
            f"""
            SELECT TOP {_limit(limit, 200, 1000)}
                J."TransId", J."RefDate", J."DueDate", J."TaxDate", J."Debit", J."Credit",
                J."LineMemo", J."BaseRef", J."ContraAct",
                COALESCE(A2."AcctName", C2."CardName"), H."Ref2", H."TransType"
            FROM "{{schema}}"."JDT1" J
            JOIN "{{schema}}"."OJDT" H ON H."TransId" = J."TransId"
            LEFT JOIN "{{schema}}"."OACT" A2 ON A2."AcctCode" = J."ContraAct"
            LEFT JOIN "{{schema}}"."OCRD" C2 ON C2."CardCode" = J."ContraAct"
            WHERE {where_sql}
            ORDER BY J."RefDate" DESC, J."TransId" DESC
            """,
            tuple(params),
        )
        running = anchor
        lines = []
        for trans_id, ref_date, due_date, tax_date, debit, credit, memo, ref, contra, contra_name, bill_no, trans_type in rows:
            debit, credit = _money(debit), _money(credit)
            lines.append(
                {
                    "trans_id": _int(trans_id),
                    "date": _date(ref_date),
                    "due_date": _date(due_date),
                    "document_date": _date(tax_date),
                    "debit": debit,
                    "credit": credit,
                    # Balance after this posting; the next row is one posting older.
                    "balance": round(running, 2),
                    "memo": _clean(memo),
                    "reference": _clean(ref),
                    "bill_no": _clean(bill_no),
                    "trans_type": _clean(trans_type),
                    "trans_type_label": TRANS_TYPE_LABELS.get(_clean(trans_type), _clean(trans_type)),
                    "offset_account": _clean(contra),
                    "offset_name": _clean(contra_name),
                }
            )
            running -= debit - credit

        return {
            "account": code,
            "kind": kind,
            "name": _clean(name),
            "balance": current_balance,
            "closing_balance": anchor,
            "currency": "INR",
            "total": total,
            "lines": lines,
        }

    def ledger_account_search(self, search: str, limit: int = 20) -> list[dict]:
        """G/L accounts and partners together — the ledger takes either code."""
        like = f"%{(search or '').strip().upper()}%"
        size = _limit(limit, 20, 100)
        accounts = self._query(
            f"""
            SELECT TOP {size} "AcctCode", "AcctName"
            FROM "{{schema}}"."OACT"
            WHERE "Postable" = 'Y' AND (UPPER("AcctCode") LIKE ? OR UPPER("AcctName") LIKE ?)
            ORDER BY "AcctCode"
            """,
            (like, like),
        )
        partners = self._query(
            f"""
            SELECT TOP {size} "CardCode", "CardName", "CardType"
            FROM "{{schema}}"."OCRD"
            WHERE "validFor" = 'Y' AND (UPPER("CardCode") LIKE ? OR UPPER("CardName") LIKE ?)
            ORDER BY "CardName"
            """,
            (like, like),
        )
        partner_kind = {"S": "Vendor", "C": "Customer"}
        return [{"code": _clean(c), "name": _clean(n), "kind": "G/L"} for c, n in accounts] + [
            {"code": _clean(c), "name": _clean(n), "kind": partner_kind.get(_clean(t), "BP")}
            for c, n, t in partners
        ]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading finance data: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e
        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA finance query failed: %s", e)
            raise SAPDataError("Failed to read finance data from SAP.") from e
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
