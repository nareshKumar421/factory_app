"""
dispatch_plans/freight_rate_service.py

Outbound freight, read from SAP and nowhere else.

This deliberately does NOT go through `ServiceGRPOPosting` / `ServiceGRPOLinePosting`.
Those rows record what this app posted, which is not the same as what SAP holds:
GRPOs are also entered directly in SAP, a posting can succeed there and fail to
write back here, and on the live books the local rows covered none of September
while SAP held 71 documents worth 15.87 lakh for the same days. A freight figure
built on them reported zero, which on a wall reads as free carriage.

What identifies outbound freight in SAP, verified on the live books:

  - `OPDN` with `DocType = 'S'` -- a SERVICE purchase delivery note, which is
    what a freight receipt is; a goods receipt would be 'I'.
  - a vendor in the `TRANSPORTER` group (`OCRG.GroupName`, matched by name so a
    company that numbered its groups differently still answers).
  - every line posting to the outward-freight expense account. All 380 of Oil's
    service GRPOs and all 140 of Mart's post to `5670001` -- "FREIGHT AND
    CARTAGE OUTWARD-INDIRECT EXP" -- so the account is what separates freight
    the plant's own dispatches incurred from inbound haulage, which has no
    dispatched litre under it and would inflate the numerator.

The money is `SUM(PDN1.LineTotal)`: the pre-tax carriage, not `OPDN.DocTotal`,
which carries GST (~2% on these books). A rate wants the carriage.

Litres are NOT here. They come from the app's own dispatch figures and the
caller divides -- two independent totals over the same window, divided once.
"""

import logging
from typing import Any, Dict, List, Sequence

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

# The vendor group a haulier belongs to, matched on the group's NAME.
TRANSPORTER_GROUP_NAME = "TRANSPORTER"

# The expense account outbound freight is booked to. Every service GRPO for a
# transporter in both companies posts here, so it is what keeps inbound haulage
# out of a rate whose denominator is DISPATCHED litres.
OUTWARD_FREIGHT_ACCOUNT = "5670001"

# A wall tile shows a handful of hauliers; the rest stay in the total.
MAX_VENDOR_ROWS = 25


class FreightRateService:
    """Outbound freight for one company, over a document-date window."""

    def __init__(self, context, company_code: str):
        self.company_code = company_code
        self.connection = HanaConnection(context.hana)
        self.schema = self.connection.schema

    # ------------------------------------------------------------------ #
    # public
    # ------------------------------------------------------------------ #
    def get_rate(self, date_from, date_to) -> Dict[str, Any]:
        """
        Freight posted in the window, in total and per haulier.

        Windowed on the GRPO's own `DocDate`. There is no dispatch date to
        anchor on once the app's posting rows are out of the picture -- SAP
        holds no link back to the bill, its `U_BilltyNumber` being filled on 17
        of 380 documents -- so the window is when the freight was received, not
        when the truck left.

        That matters, and the caller is told: freight is received days to weeks
        after the dispatch it pays for, so a month-to-date numerator lags a
        month-to-date denominator and the rate reads LOW until the month catches
        up. It settles as the paperwork lands rather than being wrong.
        """
        query = f"""
            SELECT
                D."CardCode",
                MAX(D."CardName") AS card_name,
                COUNT(DISTINCT D."DocEntry") AS docs,
                SUM(L."LineTotal") AS freight
            FROM "{self.schema}"."OPDN" D
            JOIN "{self.schema}"."PDN1" L ON L."DocEntry" = D."DocEntry"
            JOIN "{self.schema}"."OCRD" C ON C."CardCode" = D."CardCode"
            JOIN "{self.schema}"."OCRG" G ON G."GroupCode" = C."GroupCode"
            WHERE G."GroupName" = ?
              AND G."GroupType" = 'S'
              AND D."DocType" = 'S'
              AND IFNULL(D."CANCELED", 'N') = 'N'
              AND L."AcctCode" = ?
              AND D."DocDate" >= ?
              AND D."DocDate" <= ?
            GROUP BY D."CardCode"
            ORDER BY SUM(L."LineTotal") DESC
        """
        rows = self._execute(
            query,
            [TRANSPORTER_GROUP_NAME, OUTWARD_FREIGHT_ACCOUNT, date_from, date_to],
        )

        vendors = [
            {
                "card_code": row[0] or "",
                "transporter_name": (row[1] or "").strip() or (row[0] or "Unassigned"),
                "documents": int(row[2] or 0),
                "amount": float(row[3] or 0),
            }
            for row in rows
        ]

        return {
            "amount": sum(vendor["amount"] for vendor in vendors),
            "documents": sum(vendor["documents"] for vendor in vendors),
            "vendors": len(vendors),
            # Dearest first, by SPEND. There is no per-haulier litre figure to
            # rank by: SAP cannot say which dispatch a freight document paid
            # for, so a haulier's share of the spend is the honest comparison
            # and a per-haulier rupees-per-litre is simply not available.
            "by_transporter": vendors[:MAX_VENDOR_ROWS],
        }

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    def _execute(self, query: str, params: List[Any]) -> List[Sequence[Any]]:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed for the freight rate: %s", e)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.ProgrammingError as e:
            logger.error("SAP HANA freight rate query error: %s", e)
            raise SAPDataError("Failed to read freight from SAP. Invalid query.") from e
        except dbapi.Error as e:
            logger.error("SAP HANA freight rate data error: %s", e)
            raise SAPDataError("Failed to read freight from SAP. Please try again.") from e
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
