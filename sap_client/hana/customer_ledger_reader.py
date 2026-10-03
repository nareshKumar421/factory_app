"""One customer's ledger: every posting to their account, oldest first, with
the balance after each — the party statement an accountant reads.

Read from the journal: the ``JDT1`` lines whose ``ShortName`` is the customer,
with their ``OJDT`` header. ``ShortName`` carries the partner's own code on the
line that hits their control account, so every document SAP books against the
customer is here — invoices, credit notes, incoming payments and their
reversals, manual journal entries, reconciliations — and nothing else.

The balances are summed from those same lines, not anchored on
``OCRD.Balance``. The two agree (on Oil every customer's ``Debit - Credit``
sums to their ``Balance``), but summing means the opening balance, the running
column and the closing balance cannot disagree with each other whatever range
is chosen — the trap the G/L ledger in ``finance_reader`` documents.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from .finance_reader import TRANS_TYPE_LABELS
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# A financial year of the busiest customer is ~2,500 postings; past this the
# screen asks for a shorter range rather than ship a statement nobody can read.
LEDGER_ROW_CAP = 5000

LEDGER_TYPE_LABELS = {**TRANS_TYPE_LABELS, "321": "Reconciliation"}

_CENT = Decimal("0.01")


def _clean(value) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value)


def _decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _amount(value: Decimal) -> float:
    return float(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _date(value) -> Optional[str]:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)


def _narration(line_memo, header_memo, card_code: str) -> str:
    """What the posting says about itself.

    SAP stamps an automatic memo on every document line — "A/R Invoices -
    CUSTA000486", "Incoming Payments - CUSTA000486" — which only repeats the
    type and the customer. The header is where an incoming payment keeps its
    bank narration ("BY TRANSFER NEFT/HSBC/…"), and a journal entry its
    remark, so the first memo that is not the automatic one wins.
    """
    automatic = f" - {card_code}"
    for text in (_clean(line_memo), _clean(header_memo)):
        if text and not text.endswith(automatic):
            return text
    return ""


class HanaCustomerLedgerReader:
    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def ledger(
        self,
        card_code: str,
        date_from=None,
        date_to=None,
        limit: int = LEDGER_ROW_CAP,
    ) -> Optional[dict]:
        """The customer's postings between two posting dates (both optional,
        both inclusive), or ``None`` when SAP has no such customer.

        ``opening_balance`` is everything posted before ``date_from``; the
        period totals and ``closing_balance`` come from an aggregate over the
        whole range, so they stay right when the rows are capped
        (``truncated``). A positive balance is a debit — the customer owes.
        """
        card_code = (card_code or "").strip()
        if not card_code:
            return None
        customer = self._fetch(
            """
            SELECT "CardCode", IFNULL("CardName", ''), IFNULL("Balance", 0)
            FROM "{schema}"."OCRD"
            WHERE "CardType" = 'C' AND "CardCode" = ?
            """,
            (card_code,),
        )
        if not customer:
            return None
        card_code, card_name, balance_today = customer[0]

        opening = Decimal("0")
        if date_from:
            before = self._fetch(
                """
                SELECT IFNULL(SUM("Debit" - "Credit"), 0)
                FROM "{schema}"."JDT1"
                WHERE "ShortName" = ? AND "RefDate" < ?
                """,
                (card_code, date_from),
            )
            opening = _decimal(before[0][0] if before else 0)

        where = ['J."ShortName" = ?']
        params: list = [card_code]
        if date_from:
            where.append('J."RefDate" >= ?')
            params.append(date_from)
        if date_to:
            where.append('J."RefDate" <= ?')
            params.append(date_to)
        where_sql = " AND ".join(where)

        summary = self._fetch(
            f"""
            SELECT COUNT(*), IFNULL(SUM(J."Debit"), 0), IFNULL(SUM(J."Credit"), 0)
            FROM "{{schema}}"."JDT1" J
            WHERE {where_sql}
            """,
            tuple(params),
        )
        count, total_debit, total_credit = summary[0] if summary else (0, 0, 0)
        count = int(count or 0)
        total_debit, total_credit = _decimal(total_debit), _decimal(total_credit)

        cap = max(1, min(int(limit or LEDGER_ROW_CAP), LEDGER_ROW_CAP))
        rows = self._fetch(
            f"""
            SELECT TOP {cap}
                J."TransId", J."Line_ID", J."RefDate", J."DueDate", H."TransType",
                J."BaseRef", J."Ref2", J."LineMemo", H."Memo",
                J."ContraAct", COALESCE(A."AcctName", C."CardName"),
                J."Debit", J."Credit", J."BalDueDeb", J."BalDueCred"
            FROM "{{schema}}"."JDT1" J
            JOIN "{{schema}}"."OJDT" H ON H."TransId" = J."TransId"
            LEFT JOIN "{{schema}}"."OACT" A ON A."AcctCode" = J."ContraAct"
            LEFT JOIN "{{schema}}"."OCRD" C ON C."CardCode" = J."ContraAct"
            WHERE {where_sql}
            ORDER BY J."RefDate", J."TransId", J."Line_ID"
            """,
            tuple(params),
        )

        running = opening
        lines = []
        for (
            trans_id, line_id, ref_date, due_date, trans_type,
            base_ref, ref2, line_memo, header_memo,
            contra, contra_name, debit, credit, due_debit, due_credit,
        ) in rows:
            debit, credit = _decimal(debit), _decimal(credit)
            running += debit - credit
            trans_type = _clean(trans_type)
            lines.append(
                {
                    "trans_id": int(trans_id),
                    "line_id": int(line_id or 0),
                    "date": _date(ref_date),
                    "due_date": _date(due_date),
                    "trans_type": trans_type,
                    "trans_type_label": LEDGER_TYPE_LABELS.get(trans_type, trans_type),
                    # The source document's own number (OINV/ORIN/ORCT DocNum).
                    "doc_num": _clean(base_ref),
                    # The customer's reference — on an invoice, their PO number.
                    "reference": _clean(ref2),
                    "narration": _narration(line_memo, header_memo, card_code),
                    "offset_account": _clean(contra),
                    "offset_name": _clean(contra_name),
                    "debit": _amount(debit),
                    "credit": _amount(credit),
                    "balance": _amount(running),
                    # SAP's balance due on the line: the part of an invoice not
                    # yet matched to a payment (positive), or of a payment or
                    # credit note not yet matched to a bill (negative).
                    "open_amount": _amount(_decimal(due_debit) - _decimal(due_credit)),
                }
            )

        return {
            "customer_code": card_code,
            "customer_name": _clean(card_name) or card_code,
            "date_from": _date(date_from),
            "date_to": _date(date_to),
            "currency": "INR",
            "opening_balance": _amount(opening),
            "total_debit": _amount(total_debit),
            "total_credit": _amount(total_credit),
            "closing_balance": _amount(opening + total_debit - total_credit),
            "balance_today": _amount(_decimal(balance_today)),
            "total": count,
            "truncated": count > len(lines),
            "lines": lines,
        }

    def _fetch(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading a customer ledger: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e
        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA customer ledger query failed: %s", e)
            raise SAPDataError("Failed to read the customer ledger from SAP.") from e
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
