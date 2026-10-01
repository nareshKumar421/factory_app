"""HANA reads behind the factory A/R-invoice module.

Mirror of ``ap_invoice_reader`` for the sales side: pick a customer's open
Sales Order lines, raise an A/R invoice (which SAP holds as an ObjType-13
approval draft), then track the draft until it becomes a posted OINV document.

Data facts (verified live):

* An A/R invoice line copied from a Sales Order lands in ``INV1`` with
  ``BaseType = 17`` and ``BaseEntry/BaseLine`` pointing at ``RDR1``; SAP also
  maintains ``RDR1.OpenQty`` directly, so open lines are simply
  ``LineStatus = 'O' AND OpenQty > 0`` — no anti-join needed. A draft pending
  approval does NOT reduce ``OpenQty`` (only the posted invoice does), so our
  own in-flight submissions are excluded app-side.
* ``OINV.draftKey`` links a posted invoice back to its approval draft.
* A/R drafts carry no batch allocations (``DRF16`` empty for ObjType 13);
  ``draft_lines`` feeds the FIFO allocation written onto the draft before it
  is added.
"""

import logging
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaARInvoiceReader:
    # How the org names its counter/cash-sale customers in the BP master —
    # "HARPREET SINGH CASH SALE" (Oil), "CASH SALE DL" (Beverages). Used to
    # discover them when no explicit CardCodes are configured; matched against
    # UPPER(CardName), so keep it upper case.
    CASH_SALE_NAME_TOKEN = "CASH SALE"
    CASH_SALE_NAME_PATTERN = f"%{CASH_SALE_NAME_TOKEN}%"

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def open_so_lines(
        self,
        card_code: str,
        search: Optional[str] = None,
        limit: int = 300,
    ) -> list[dict]:
        """One customer's open Sales Order lines (invoiceable open quantity)."""
        card_code = (card_code or "").strip()
        if not card_code:
            return []
        safe_limit = max(1, min(int(limit or 300), 500))

        where = [
            """IFNULL(H."CANCELED", 'N') = 'N'""",
            """H."DocStatus" = 'O'""",
            """H."CardCode" = ?""",
            """IFNULL(L."LineStatus", 'O') = 'O'""",
            """IFNULL(L."OpenQty", 0) > 0""",
        ]
        params: list = [card_code]
        if search:
            term = f"%{search.lower()}%"
            where.append(
                """(
                    LOWER(TO_NVARCHAR(H."DocNum")) LIKE ?
                    OR LOWER(IFNULL(H."NumAtCard", '')) LIKE ?
                    OR LOWER(IFNULL(H."Comments", '')) LIKE ?
                    OR LOWER(IFNULL(L."ItemCode", '')) LIKE ?
                    OR LOWER(IFNULL(L."Dscription", '')) LIKE ?
                )"""
            )
            params.extend([term] * 5)

        rows = self._query(
            f"""
            SELECT
                H."DocEntry", H."DocNum", H."DocDate",
                IFNULL(H."NumAtCard", ''), IFNULL(H."Comments", ''),
                H."BPLId", IFNULL(H."CardName", ''),
                L."LineNum", IFNULL(L."ItemCode", ''), IFNULL(L."Dscription", ''),
                L."OpenQty", IFNULL(L."Price", 0),
                IFNULL(L."OpenSum", L."OpenQty" * IFNULL(L."Price", 0)),
                IFNULL(NULLIF(L."TaxCode", ''), IFNULL(L."VatGroup", '')),
                IFNULL(L."WhsCode", ''), IFNULL(L."unitMsr", '')
            FROM "{{schema}}"."ORDR" H
            JOIN "{{schema}}"."RDR1" L ON L."DocEntry" = H."DocEntry"
            WHERE {" AND ".join(where)}
            ORDER BY H."DocDate" DESC, H."DocEntry" DESC, L."LineNum"
            LIMIT {safe_limit}
            """,
            tuple(params),
        )

        lines = []
        for (
            doc_entry, doc_num, doc_date, num_at_card, comments,
            branch_id, card_name,
            line_num, item_code, description,
            open_qty, price, open_total, tax_code, whs_code, uom,
        ) in rows:
            lines.append({
                "so_doc_entry": int(doc_entry),
                "so_doc_num": int(doc_num) if doc_num is not None else None,
                "so_doc_date": self._date(doc_date),
                "so_customer_ref": num_at_card or "",
                "so_comments": comments or "",
                "branch_id": int(branch_id) if branch_id is not None else None,
                "customer_name": card_name or "",
                "line_num": int(line_num),
                "item_code": item_code or "",
                "description": description or "",
                # Invoicing a base SO line without a Quantity copies the OPEN
                # quantity — this is what the invoice will carry.
                "open_qty": float(open_qty) if open_qty is not None else 0.0,
                "price": float(price or 0),
                "open_total": float(open_total or 0),
                "tax_code": tax_code or "",
                "warehouse_code": whs_code or "",
                "uom": uom or "",
            })
        return lines

    def invoice_for_draft(self, draft_entry: int) -> Optional[dict]:
        """The posted OINV invoice created from one approval draft, if any."""
        rows = self._query(
            """
            SELECT "DocEntry", "DocNum", "DocTotal"
            FROM "{schema}"."OINV"
            WHERE "draftKey" = ? AND IFNULL("CANCELED", 'N') = 'N'
            LIMIT 1
            """,
            (int(draft_entry),),
        )
        if not rows:
            return None
        doc_entry, doc_num, doc_total = rows[0]
        return {
            "doc_entry": int(doc_entry),
            "doc_num": int(doc_num) if doc_num is not None else None,
            "doc_total": float(doc_total or 0),
        }

    def draft_state(self, draft_entry: int) -> Optional[dict]:
        """The draft's document status plus the state of its approval request(s).

        A draft matching two approval templates opens one CONCURRENT request per
        template (``OWDD.WtmCode``), each with its own authorizer and each needing
        its own decision — 871 of Oil's 10,304 ObjType-13 drafts. So the latest
        request is taken per TEMPLATE and the states are folded worst-first: any
        rejection rejects the invoice, any request still waiting keeps it pending,
        and it counts as approved only once every request is. Reading just the
        highest WddCode called an invoice approved while a second approver had not
        yet seen it.
        """
        rows = self._query(
            """
            SELECT
                D."DocStatus", D."WddStatus", D."DocTotal",
                W."WddCode", W."Status",
                (SELECT MAX(S."Remarks") FROM "{schema}"."WDD1" S
                 WHERE S."WddCode" = W."WddCode" AND S."Status" = 'N')
            FROM "{schema}"."ODRF" D
            LEFT JOIN "{schema}"."OWDD" W
                ON W."DraftEntry" = D."DocEntry" AND W."ObjType" = '13'
               AND W."WddCode" = (
                    SELECT MAX(W2."WddCode") FROM "{schema}"."OWDD" W2
                    WHERE W2."DraftEntry" = D."DocEntry" AND W2."ObjType" = '13'
                      AND W2."WtmCode" = W."WtmCode"
               )
            WHERE D."DocEntry" = ? AND D."ObjType" = '13'
            ORDER BY W."WddCode"
            """,
            (int(draft_entry),),
        )
        if not rows:
            return None
        doc_status, wdd_status, doc_total = rows[0][0], rows[0][1], rows[0][2]
        requests = [
            (int(code), status, remarks)
            for _, _, _, code, status, remarks in rows
            if code is not None
        ]
        # Worst state wins, and the code reported is the request that owns it:
        # the rejection to show, else the request still to be decided, else the
        # last approval.
        governing = (
            next((r for r in requests if r[1] == "N"), None)
            or next((r for r in requests if r[1] == "W"), None)
            or (requests[-1] if requests else None)
        )
        return {
            "doc_status": doc_status,
            "wdd_status": wdd_status,
            "doc_total": float(doc_total or 0),
            "approval_code": governing[0] if governing else None,
            # 'W' waiting / 'Y' approved / 'N' rejected
            "approval_status": governing[1] if governing else None,
            "approval_codes": [code for code, _, _ in requests],
            "reject_remarks": (governing[2] or None) if governing else None,
        }

    def draft_lines(self, draft_entry: int) -> list[dict]:
        """The draft's own lines (DRF1) — the authoritative LineNum/quantity/
        warehouse set the batch allocation must be written against."""
        rows = self._query(
            """
            SELECT L."LineNum", IFNULL(L."ItemCode", ''), L."Quantity",
                   IFNULL(L."WhsCode", '')
            FROM "{schema}"."DRF1" L
            JOIN "{schema}"."ODRF" D
                ON D."DocEntry" = L."DocEntry" AND D."ObjType" = '13'
            WHERE L."DocEntry" = ?
            ORDER BY L."LineNum"
            """,
            (int(draft_entry),),
        )
        return [
            {
                "line_num": int(line_num),
                "item_code": item_code or "",
                "quantity": float(quantity) if quantity is not None else 0.0,
                "warehouse_code": whs_code or "",
            }
            for line_num, item_code, quantity, whs_code in rows
        ]

    def line_price_guide(
        self, card_code: str, item_code: str, recent_limit: int = 8
    ) -> dict:
        """What a direct (cash) sale line's price starts from, and the bills
        around it.

        ``price`` (pre-tax) prefills the line: the customer's price list when it
        prices the item, else what the customer last paid. ``recent`` is the
        item's latest bills to anyone, so the operator sees the going rate next
        to the prefill — the customer's own last bill can be long out of date
        (a ₹140 pouch from July 2025 was prefilling counter sales that now go
        at ₹160). The tax code still comes from the customer's last bill: it
        follows their place of supply, which another customer's bill does not.
        """
        card_code = (card_code or "").strip()
        item_code = (item_code or "").strip()
        guide = {
            "price": None,
            "tax_code": "",
            "source": None,
            "price_list": None,
            "last_sale": None,
            "recent": [],
        }
        if not card_code or not item_code:
            return guide
        safe_limit = max(1, min(int(recent_limit or 8), 50))

        # The item's latest lines to anyone, plus this customer's latest even
        # when it is older than all of those.
        rows = self._query(
            """
            SELECT
                "DocEntry", "DocNum", "DocDate", "CardCode", "CardName",
                "Quantity", "Price", "PriceAfVAT", "TaxCode", "WhsCode",
                "rn_all", "rn_bp"
            FROM (
                SELECT
                    H."DocEntry", H."DocNum", H."DocDate", H."CardCode",
                    IFNULL(H."CardName", '') AS "CardName",
                    IFNULL(L."Quantity", 0) AS "Quantity",
                    L."Price", L."PriceAfVAT",
                    IFNULL(NULLIF(L."TaxCode", ''), IFNULL(L."VatGroup", '')) AS "TaxCode",
                    IFNULL(L."WhsCode", '') AS "WhsCode",
                    ROW_NUMBER() OVER (
                        ORDER BY H."DocDate" DESC, H."DocEntry" DESC, L."LineNum" DESC
                    ) AS "rn_all",
                    ROW_NUMBER() OVER (
                        PARTITION BY H."CardCode"
                        ORDER BY H."DocDate" DESC, H."DocEntry" DESC, L."LineNum" DESC
                    ) AS "rn_bp"
                FROM "{schema}"."OINV" H
                JOIN "{schema}"."INV1" L ON L."DocEntry" = H."DocEntry"
                WHERE L."ItemCode" = ?
                  AND IFNULL(H."CANCELED", 'N') = 'N'
            )
            WHERE "rn_all" <= ? OR ("CardCode" = ? AND "rn_bp" = 1)
            ORDER BY "rn_all"
            """,
            (item_code, safe_limit, card_code),
        )
        for (
            doc_entry, doc_num, doc_date, row_card, row_name, quantity,
            price, price_incl_tax, tax_code, whs_code, rn_all, rn_bp,
        ) in rows:
            sale = {
                "doc_entry": int(doc_entry),
                "doc_num": int(doc_num) if doc_num is not None else None,
                "doc_date": self._date(doc_date),
                "customer_code": row_card or "",
                "customer_name": row_name or "",
                "quantity": float(quantity or 0),
                "price": float(price) if price is not None else None,
                "price_incl_tax": (
                    float(price_incl_tax) if price_incl_tax is not None else None
                ),
                "tax_code": tax_code or "",
                "warehouse_code": whs_code or "",
            }
            if int(rn_all) <= safe_limit:
                guide["recent"].append(sale)
            if row_card == card_code and int(rn_bp) == 1:
                guide["last_sale"] = sale
                guide["tax_code"] = sale["tax_code"]

        # The customer's price list, and the tax rate a tax-inclusive list's
        # price has to come off.
        list_rows = self._query(
            """
            SELECT C."ListNum", IFNULL(P."ListName", ''), IFNULL(P."IsGrossPrc", 'N'),
                   I."Price", T."Rate"
            FROM "{schema}"."OCRD" C
            LEFT JOIN "{schema}"."OPLN" P ON P."ListNum" = C."ListNum"
            LEFT JOIN "{schema}"."ITM1" I
                   ON I."PriceList" = C."ListNum" AND I."ItemCode" = ?
            LEFT JOIN "{schema}"."OSTC" T ON T."Code" = ?
            WHERE C."CardCode" = ?
            """,
            (item_code, guide["tax_code"], card_code),
        )
        if list_rows:
            list_num, list_name, gross, list_price, rate = list_rows[0]
            # SAP leaves an unpriced item at 0 on every list, not NULL.
            if list_price is not None and float(list_price) > 0:
                includes_tax = (gross or "N") == "Y"
                if not includes_tax:
                    net_price = float(list_price)
                elif rate is not None:
                    net_price = round(float(list_price) / (1 + float(rate) / 100), 4)
                else:
                    net_price = None  # no tax code to take it off
                guide["price_list"] = {
                    "list_num": int(list_num),
                    "list_name": list_name or "",
                    "price": float(list_price),
                    "includes_tax": includes_tax,
                    "net_price": net_price,
                }

        if guide["price_list"] and guide["price_list"]["net_price"] is not None:
            guide["price"] = guide["price_list"]["net_price"]
            guide["source"] = "price_list"
        elif guide["last_sale"] and guide["last_sale"]["price"] is not None:
            guide["price"] = guide["last_sale"]["price"]
            guide["source"] = "last_sale"
        return guide

    def cash_sale_invoices(
        self,
        card_codes: Optional[list[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        """The counter/cash-sale book as SAP holds it, newest first, with lines.

        The app raises cash sales, but the counter has always raised them in SAP
        directly too, so the app's own History is only part of the day's book.
        These are read live rather than mirrored: SAP remains the source of
        truth for a document we did not create and can still amend.

        Cancelled invoices are included and flagged — a voided cash sale is part
        of the counter's day, and hiding it reads as a bill that never existed.
        """
        safe_limit = max(1, min(int(limit or 500), 1000))
        codes = [str(c).strip() for c in (card_codes or []) if str(c).strip()]

        where: list[str] = []
        params: list = []
        if codes:
            where.append(f"""H."CardCode" IN ({", ".join(["?"] * len(codes))})""")
            params.extend(codes)
        else:
            # No codes configured: fall back to the org's own naming for these
            # accounts. Matching on the BP *name* is safe here only because it
            # selects which customers to show, not any billed value.
            where.append("""UPPER(IFNULL(C."CardName", '')) LIKE ?""")
            params.append(self.CASH_SALE_NAME_PATTERN)
        if date_from:
            where.append("""H."DocDate" >= TO_DATE(?, 'YYYY-MM-DD')""")
            params.append(str(date_from))
        if date_to:
            where.append("""H."DocDate" <= TO_DATE(?, 'YYYY-MM-DD')""")
            params.append(str(date_to))
        if search:
            term = f"%{search.lower()}%"
            where.append(
                """(
                    LOWER(TO_NVARCHAR(H."DocNum")) LIKE ?
                    OR LOWER(IFNULL(H."NumAtCard", '')) LIKE ?
                    OR LOWER(IFNULL(H."Comments", '')) LIKE ?
                    OR LOWER(IFNULL(H."CardName", '')) LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM "{schema}"."INV1" S
                        WHERE S."DocEntry" = H."DocEntry"
                          AND (
                            LOWER(IFNULL(S."ItemCode", '')) LIKE ?
                            OR LOWER(IFNULL(S."Dscription", '')) LIKE ?
                          )
                    )
                )"""
            )
            params.extend([term] * 6)

        rows = self._query(
            f"""
            SELECT
                H."DocEntry", H."DocNum", H."DocDate", H."DocDueDate", H."TaxDate",
                H."CardCode", IFNULL(H."CardName", ''),
                IFNULL(H."NumAtCard", ''), IFNULL(H."Comments", ''),
                IFNULL(H."DocTotal", 0), IFNULL(H."VatSum", 0),
                IFNULL(H."PaidToDate", 0),
                IFNULL(H."DocStatus", 'O'), IFNULL(H."CANCELED", 'N'),
                H."BPLId", IFNULL(B."BPLName", ''),
                IFNULL(U."U_NAME", IFNULL(U."USER_CODE", '')),
                H."draftKey", H."CreateDate"
            FROM "{{schema}}"."OINV" H
            JOIN "{{schema}}"."OCRD" C ON C."CardCode" = H."CardCode"
            LEFT JOIN "{{schema}}"."OBPL" B ON B."BPLId" = H."BPLId"
            LEFT JOIN "{{schema}}"."OUSR" U ON U."USERID" = H."UserSign"
            WHERE {" AND ".join(where)}
            ORDER BY H."DocDate" DESC, H."DocEntry" DESC
            LIMIT {safe_limit}
            """,
            tuple(params),
        )

        invoices = []
        by_entry = {}
        for (
            doc_entry, doc_num, doc_date, due_date, tax_date,
            card_code, card_name, num_at_card, comments,
            doc_total, vat_sum, paid_to_date,
            doc_status, canceled, branch_id, branch_name,
            sap_user, draft_key, create_date,
        ) in rows:
            invoice = {
                "doc_entry": int(doc_entry),
                "doc_num": int(doc_num) if doc_num is not None else None,
                "doc_date": self._date(doc_date),
                "doc_due_date": self._date(due_date),
                "tax_date": self._date(tax_date),
                "created_date": self._date(create_date),
                "customer_code": card_code or "",
                "customer_name": card_name or "",
                "customer_ref": num_at_card or "",
                "comments": comments or "",
                "doc_total": float(doc_total or 0),
                "tax_total": float(vat_sum or 0),
                "paid_to_date": float(paid_to_date or 0),
                # 'O' open, 'C' closed — a cash sale stays open until a receipt
                # is applied to it, so this is not "unpaid at the counter".
                "doc_status": doc_status or "O",
                "is_cancelled": (canceled or "N") == "Y",
                "branch_id": int(branch_id) if branch_id is not None else None,
                "branch_name": branch_name or "",
                # Whoever keyed it in SAP (OINV.UserSign), blank for ours.
                "sap_user": sap_user or "",
                "draft_entry": int(draft_key) if draft_key else None,
                "lines": [],
            }
            invoices.append(invoice)
            by_entry[invoice["doc_entry"]] = invoice

        if not by_entry:
            return []

        entries = list(by_entry)
        line_rows = self._query(
            f"""
            SELECT
                L."DocEntry", L."LineNum", IFNULL(L."ItemCode", ''),
                IFNULL(L."Dscription", ''), IFNULL(L."Quantity", 0),
                IFNULL(L."Price", 0), IFNULL(L."LineTotal", 0),
                IFNULL(NULLIF(L."TaxCode", ''), IFNULL(L."VatGroup", '')),
                IFNULL(L."WhsCode", ''), IFNULL(L."unitMsr", ''),
                IFNULL(L."OcrCode", '')
            FROM "{{schema}}"."INV1" L
            WHERE L."DocEntry" IN ({", ".join(["?"] * len(entries))})
            ORDER BY L."DocEntry", L."LineNum"
            """,
            tuple(entries),
        )
        for (
            doc_entry, line_num, item_code, description, quantity,
            price, line_total, tax_code, whs_code, uom, cost_center,
        ) in line_rows:
            by_entry[int(doc_entry)]["lines"].append({
                "line_num": int(line_num),
                "item_code": item_code or "",
                "description": description or "",
                "quantity": float(quantity or 0),
                "price": float(price or 0),
                "line_total": float(line_total or 0),
                "tax_code": tax_code or "",
                "warehouse_code": whs_code or "",
                "uom": uom or "",
                "cost_center": cost_center or "",
            })
        return invoices

    def cash_sale_invoice_state(
        self, doc_entry: int, card_codes: Optional[list[str]] = None
    ) -> Optional[dict]:
        """Whether one posted invoice belongs to the cash-sale book, and its state.

        The cash-sale screen prints by ``DocEntry`` — the counter's own bills
        have no record in this app to print from — so the print path asks this
        first. It answers the two things that decide whether a sheet should come
        out at all: the invoice is one of the cash-sale customers' (the book the
        screen lists, not every invoice in the company), and SAP has not
        cancelled it. ``None`` means the company has no such invoice.

        The cash-sale test mirrors ``cash_sale_invoices`` exactly: the configured
        CardCodes when there are any, the BP naming otherwise.
        """
        rows = self._query(
            """
            SELECT
                H."DocNum", IFNULL(H."CANCELED", 'N'),
                H."CardCode", UPPER(IFNULL(C."CardName", ''))
            FROM "{schema}"."OINV" H
            JOIN "{schema}"."OCRD" C ON C."CardCode" = H."CardCode"
            WHERE H."DocEntry" = ?
            """,
            (int(doc_entry),),
        )
        if not rows:
            return None

        doc_num, canceled, card_code, card_name = rows[0]
        codes = [str(c).strip() for c in (card_codes or []) if str(c).strip()]
        if codes:
            is_cash_sale = (card_code or "") in codes
        else:
            is_cash_sale = self.CASH_SALE_NAME_TOKEN in (card_name or "")

        return {
            "doc_entry": int(doc_entry),
            "doc_num": int(doc_num) if doc_num is not None else None,
            "customer_code": card_code or "",
            "is_cash_sale": is_cash_sale,
            "is_cancelled": (canceled or "N") == "Y",
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _date(value):
        if value is None:
            return None
        if hasattr(value, "date"):
            value = value.date()
        return value.strftime("%Y-%m-%d")

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading A/R invoices: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA A/R invoice query failed: %s", e)
            raise SAPDataError("Failed to read A/R invoice data from SAP.") from e
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
