"""
dispatch_plans/transporter_account_reader.py

What one company still owes its transporters, and what it has paid them.

Read-only, straight off SAP B1's HANA tables. Nothing in this repo tracked a
freight payment before this reader: `TransporterAPInvoicePosting` records the
invoice we posted and stops there, with no paid-to-date and no payment status,
so "how much is still outstanding" had no source at all and the board that asked
the question drew a rule where the number goes.

Three decisions are baked in here, and each one is silent when wrong:

  1. **A transporter is a vendor in the TRANSPORTER group** — `OCRD.GroupCode`,
     resolved by NAME ('TRANSPORTER' in OCRG) rather than the literal 102, so a
     company that numbered its groups differently still answers correctly.
     Filtering on the group rather than on the CardCodes this app has posted to
     is deliberate: freight invoices are also entered directly in SAP by
     accounts, and a dashboard headed "transporter account" that showed only the
     ones this app created would understate the payable by most of its value.

  2. **Outstanding is `DocTotal - PaidToDate` on OPEN invoices**, never the
     vendor's OCRD balance. The balance folds in down payments, credit notes and
     anything else posted to the BP, and it carries a sign convention that reads
     backwards half the time. Document-level arithmetic is the figure an accounts
     desk can tie back to a list of invoices.

  3. **Ageing runs from `DocDate`, not `DocDueDate`.** Measured on the live
     books, every open transporter invoice in both companies is already past its
     due date — zero not-yet-due — so due-date ageing would collapse to a single
     bucket and say nothing. Document age is what the board's age bands mean
     everywhere else, so this matches them.

The bilty is NOT the unit here. SAP has the user fields for it — `U_BilltyNumber`,
`U_GRNdocentry`, `U_DisDE` on OPCH — but on the live books they are populated on
3 of 387 open transporter invoices, so a per-bilty join would silently drop 99%
of the money. The open A/P invoice is the unit of "pending payment" instead, and
it is the better one: it is what somebody actually pays.
"""

import logging
from typing import Any, Dict, List, Sequence

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

# The vendor group a transporter belongs to, matched on the group's NAME.
TRANSPORTER_GROUP_NAME = "TRANSPORTER"

# Age bands for the outstanding split, in days since the invoice date. Floors,
# and cumulative when the caller reads them that way -- this reader returns
# EXCLUSIVE buckets that sum to the total, and leaves it to the caller to
# accumulate. Exclusive here because a payload whose parts sum to the whole can
# be turned into cumulative bands, and a cumulative one cannot be turned back.
AGE_BANDS = (15, 30, 45)

# How far back the payments window looks by default.
DEFAULT_PAYMENT_DAYS = 30

# Ceiling on the per-vendor breakdown. The whole point of the tile is the few
# names that carry the money -- on the live books two card codes carry 78% of it
# -- and a wall board cannot render ninety-odd rows anyway.
MAX_VENDOR_ROWS = 25


class HanaTransporterAccountReader:
    """Open freight payables and freight payments, for one company."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)
        # Off the connection, not off the context: `CompanyContext.hana` is the
        # raw config DICT, so `context.hana.schema` is an AttributeError rather
        # than the schema name. HanaConnection has already pulled it out.
        self.schema = self.connection.schema

    # ------------------------------------------------------------------ #
    # public
    # ------------------------------------------------------------------ #
    def get_account(self, payment_days: int = DEFAULT_PAYMENT_DAYS) -> Dict[str, Any]:
        """Everything the board needs, in four queries."""
        payment_days = max(1, int(payment_days or DEFAULT_PAYMENT_DAYS))

        return {
            "awaiting_invoice": self._awaiting_invoice(),
            "outstanding": self._outstanding(),
            "vendors": self._by_vendor(),
            "payments": self._payments(payment_days),
        }

    # ------------------------------------------------------------------ #
    # awaiting an A/P invoice
    # ------------------------------------------------------------------ #
    def _awaiting_invoice(self) -> Dict[str, Any]:
        """
        Freight received into SAP that nobody has raised an invoice against.

        The document is the service GRPO -- `OPDN` with `DocType = 'S'`, which is
        what this app posts when a bilty is received. Open means SAP has not
        copied it forward to an A/P invoice.

        The amount is `PDN1.OpenSum` over the lines that are themselves open, not
        the header `DocTotal`. Three reasons, and the middle one is the reason
        the header is actually wrong rather than merely coarse:

          - A GRPO can be part-invoiced. The header stays open while some of its
            lines are closed, so a header total counts money already invoiced as
            still awaiting an invoice.
          - `OpenSum` is the un-invoiced remainder SAP itself would carry onto
            the invoice, which is exactly the question.
          - Measured on the live books the gap is real but not huge -- Oil's open
            GRPOs come to 30.8L of line totals and 29.7L still open -- so the
            error would have been quiet enough to survive a long time.

        One honest difference from the payment stage, stated on the tile: this
        is a PRE-TAX line sum, while an open A/P invoice is a tax-inclusive
        document total. Each is the right measure for its own question -- what is
        left to invoice, versus what is owed -- and they are not addable.
        """
        query = f"""
            SELECT
                CASE
                    WHEN DAYS_BETWEEN(D."DocDate", CURRENT_DATE) >= {AGE_BANDS[2]} THEN {AGE_BANDS[2]}
                    WHEN DAYS_BETWEEN(D."DocDate", CURRENT_DATE) >= {AGE_BANDS[1]} THEN {AGE_BANDS[1]}
                    WHEN DAYS_BETWEEN(D."DocDate", CURRENT_DATE) >= {AGE_BANDS[0]} THEN {AGE_BANDS[0]}
                    ELSE 0
                END AS band,
                COUNT(DISTINCT D."DocEntry") AS docs,
                SUM(L."OpenSum") AS open_sum,
                MAX(DAYS_BETWEEN(D."DocDate", CURRENT_DATE)) AS oldest_days
            FROM "{self.schema}"."OPDN" D
            JOIN "{self.schema}"."PDN1" L
              ON L."DocEntry" = D."DocEntry" AND L."LineStatus" = 'O'
            JOIN "{self.schema}"."OCRD" C ON C."CardCode" = D."CardCode"
            JOIN "{self.schema}"."OCRG" G ON G."GroupCode" = C."GroupCode"
            WHERE G."GroupName" = ?
              AND G."GroupType" = 'S'
              AND D."DocStatus" = 'O'
              AND IFNULL(D."CANCELED", 'N') = 'N'
            GROUP BY
                CASE
                    WHEN DAYS_BETWEEN(D."DocDate", CURRENT_DATE) >= {AGE_BANDS[2]} THEN {AGE_BANDS[2]}
                    WHEN DAYS_BETWEEN(D."DocDate", CURRENT_DATE) >= {AGE_BANDS[1]} THEN {AGE_BANDS[1]}
                    WHEN DAYS_BETWEEN(D."DocDate", CURRENT_DATE) >= {AGE_BANDS[0]} THEN {AGE_BANDS[0]}
                    ELSE 0
                END
        """
        rows = self._execute(query, [TRANSPORTER_GROUP_NAME])

        buckets = {band: self._empty_awaiting_bucket(band) for band in (0,) + AGE_BANDS}
        for row in rows:
            band = int(row[0] or 0)
            buckets[band] = {
                "band": band,
                "documents": int(row[1] or 0),
                "amount": float(row[2] or 0),
                "oldest_days": int(row[3]) if row[3] is not None else None,
            }

        ordered = [buckets[band] for band in (0,) + AGE_BANDS]
        oldest = [b["oldest_days"] for b in ordered if b["oldest_days"] is not None]

        return {
            "documents": sum(b["documents"] for b in ordered),
            "amount": sum(b["amount"] for b in ordered),
            "oldest_days": max(oldest) if oldest else None,
            "buckets": ordered,
        }

    # ------------------------------------------------------------------ #
    # outstanding
    # ------------------------------------------------------------------ #
    def _outstanding(self) -> Dict[str, Any]:
        """Open freight invoices, split into exclusive age buckets."""
        query = f"""
            SELECT
                CASE
                    WHEN DAYS_BETWEEN(H."DocDate", CURRENT_DATE) >= {AGE_BANDS[2]} THEN {AGE_BANDS[2]}
                    WHEN DAYS_BETWEEN(H."DocDate", CURRENT_DATE) >= {AGE_BANDS[1]} THEN {AGE_BANDS[1]}
                    WHEN DAYS_BETWEEN(H."DocDate", CURRENT_DATE) >= {AGE_BANDS[0]} THEN {AGE_BANDS[0]}
                    ELSE 0
                END AS band,
                COUNT(*) AS docs,
                SUM(H."DocTotal" - H."PaidToDate") AS outstanding,
                SUM(H."DocTotal") AS billed,
                SUM(H."PaidToDate") AS paid,
                MAX(DAYS_BETWEEN(H."DocDate", CURRENT_DATE)) AS oldest_days
            FROM "{self.schema}"."OPCH" H
            JOIN "{self.schema}"."OCRD" C ON C."CardCode" = H."CardCode"
            JOIN "{self.schema}"."OCRG" G ON G."GroupCode" = C."GroupCode"
            WHERE G."GroupName" = ?
              AND G."GroupType" = 'S'
              AND H."DocStatus" = 'O'
              AND IFNULL(H."CANCELED", 'N') = 'N'
            GROUP BY
                CASE
                    WHEN DAYS_BETWEEN(H."DocDate", CURRENT_DATE) >= {AGE_BANDS[2]} THEN {AGE_BANDS[2]}
                    WHEN DAYS_BETWEEN(H."DocDate", CURRENT_DATE) >= {AGE_BANDS[1]} THEN {AGE_BANDS[1]}
                    WHEN DAYS_BETWEEN(H."DocDate", CURRENT_DATE) >= {AGE_BANDS[0]} THEN {AGE_BANDS[0]}
                    ELSE 0
                END
        """
        rows = self._execute(query, [TRANSPORTER_GROUP_NAME])

        buckets = {band: self._empty_bucket(band) for band in (0,) + AGE_BANDS}
        for row in rows:
            band = int(row[0] or 0)
            buckets[band] = {
                "band": band,
                "documents": int(row[1] or 0),
                "outstanding": float(row[2] or 0),
                "billed": float(row[3] or 0),
                "paid": float(row[4] or 0),
                "oldest_days": int(row[5]) if row[5] is not None else None,
            }

        ordered = [buckets[band] for band in (0,) + AGE_BANDS]
        oldest = [b["oldest_days"] for b in ordered if b["oldest_days"] is not None]

        return {
            "documents": sum(b["documents"] for b in ordered),
            "outstanding": sum(b["outstanding"] for b in ordered),
            "billed": sum(b["billed"] for b in ordered),
            # Part-paid invoices are the reason this is worth reporting: an
            # invoice can be open and still have most of its value settled.
            "paid_against_open": sum(b["paid"] for b in ordered),
            "oldest_days": max(oldest) if oldest else None,
            "buckets": ordered,
        }

    # ------------------------------------------------------------------ #
    # per vendor
    # ------------------------------------------------------------------ #
    def _by_vendor(self) -> List[Dict[str, Any]]:
        """Who the money is owed to, heaviest first."""
        query = f"""
            SELECT TOP {MAX_VENDOR_ROWS}
                H."CardCode",
                MAX(C."CardName") AS card_name,
                COUNT(*) AS docs,
                SUM(H."DocTotal" - H."PaidToDate") AS outstanding,
                MAX(DAYS_BETWEEN(H."DocDate", CURRENT_DATE)) AS oldest_days
            FROM "{self.schema}"."OPCH" H
            JOIN "{self.schema}"."OCRD" C ON C."CardCode" = H."CardCode"
            JOIN "{self.schema}"."OCRG" G ON G."GroupCode" = C."GroupCode"
            WHERE G."GroupName" = ?
              AND G."GroupType" = 'S'
              AND H."DocStatus" = 'O'
              AND IFNULL(H."CANCELED", 'N') = 'N'
            GROUP BY H."CardCode"
            ORDER BY SUM(H."DocTotal" - H."PaidToDate") DESC
        """
        rows = self._execute(query, [TRANSPORTER_GROUP_NAME])

        return [
            {
                "card_code": row[0] or "",
                "card_name": row[1] or "",
                "documents": int(row[2] or 0),
                "outstanding": float(row[3] or 0),
                "oldest_days": int(row[4]) if row[4] is not None else None,
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # payments
    # ------------------------------------------------------------------ #
    def _payments(self, days: int) -> Dict[str, Any]:
        """
        Money actually paid out to transporters in the window.

        `DocType = 'S'` only, and joined through OCRD. An outgoing payment can
        also be booked straight to a G/L account (`DocType = 'A'`), and those
        carry an account code in `CardCode` with no row in OCRD at all -- so a
        freight bill settled that way cannot be attributed to a transporter by
        any means available here, and is not in this figure. The caller says so
        on the tile rather than implying the number is every rupee of freight
        that left the bank.

        `Canceled` with one L: on OVPM and ORCT that is the spelling, while the
        sales documents use `CANCELED`. Getting it wrong here does not error --
        it silently includes reversed payments.
        """
        query = f"""
            SELECT
                COUNT(*) AS payments,
                SUM(V."DocTotal") AS paid,
                MAX(V."DocDate") AS latest
            FROM "{self.schema}"."OVPM" V
            JOIN "{self.schema}"."OCRD" C ON C."CardCode" = V."CardCode"
            JOIN "{self.schema}"."OCRG" G ON G."GroupCode" = C."GroupCode"
            WHERE G."GroupName" = ?
              AND G."GroupType" = 'S'
              AND V."DocType" = 'S'
              AND IFNULL(V."Canceled", 'N') = 'N'
              AND V."DocDate" >= ADD_DAYS(CURRENT_DATE, ?)
        """
        rows = self._execute(query, [TRANSPORTER_GROUP_NAME, -days])
        row = rows[0] if rows else None

        return {
            "window_days": days,
            "payments": int(row[0] or 0) if row else 0,
            "paid": float(row[1] or 0) if row else 0.0,
            "latest_date": self._format_date(row[2]) if row else None,
        }

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _empty_awaiting_bucket(band: int) -> Dict[str, Any]:
        return {"band": band, "documents": 0, "amount": 0.0, "oldest_days": None}

    @staticmethod
    def _empty_bucket(band: int) -> Dict[str, Any]:
        return {
            "band": band,
            "documents": 0,
            "outstanding": 0.0,
            "billed": 0.0,
            "paid": 0.0,
            "oldest_days": None,
        }

    @staticmethod
    def _format_date(value) -> str | None:
        if not value:
            return None
        try:
            return value.strftime("%Y-%m-%d")
        except AttributeError:
            return str(value)[:10]

    def _execute(self, query: str, params: List[Any]) -> List[Sequence[Any]]:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed for transporter account: %s", e)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.ProgrammingError as e:
            logger.error("SAP HANA transporter account query error: %s", e)
            raise SAPDataError(
                "Failed to read the transporter account from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error("SAP HANA transporter account data error: %s", e)
            raise SAPDataError(
                "Failed to read the transporter account from SAP. Please try again."
            ) from e
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
