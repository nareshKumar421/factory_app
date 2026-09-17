"""Everything SAP's own Goods Receipt Note layout prints, read straight from HANA.

The warehouse posts GRPOs from this app, but the sheet the stores and the vendor
know is SAP's Crystal "Goods Receipt Note". This reader reproduces that layout's
data source rather than inventing one: the fields below were taken from the
stored procedure the layout actually runs, ``CRYSTAL_GRPO_ITEM`` (one per company
schema), read out of ``SYS.PROCEDURES`` and mirrored here, then checked field by
field against a SAP-printed sheet (Beverages GRPO 2026088346, DocEntry 10462).
Where this file looks eccentric, the procedure is why.

The parts of that mapping worth knowing before changing anything:

* **"Top 3 Price" is the literal number 9.** The procedure selects
  ``'9' AS "last_price"`` — a stub left where a last-purchase-price lookup was
  meant to go — so every SAP-printed GRPO carries a 9 in that column whatever
  the item cost last time. Reproduced, so the two sheets agree; see
  ``ar_invoice_print_reader`` for the same decision about Vehicle No.

* **"Reff. Po Date" has no data behind it.** The procedure selects no purchase
  order date at all, so SAP prints the label and an empty cell. The base PO's
  date is knowable (``POR1.BaseEntry -> OPOR.DocDate``) and deliberately not
  read: a GRPO printed here that carried a date where SAP's carries none is a
  difference somebody has to reconcile.

* **The supplier's TIN, CST and PAN are empty by construction.** All five
  ``Supl*No`` fields are ``CAST('' AS NVARCHAR)`` constants in the procedure.
  The labels print; the values never do.

* **"Supplier GST No" is the ship-from address's GSTIN, not the vendor master's.**
  It comes from ``CRD1`` on the document's own ``ShipToCode`` with
  ``AdresType = 'S'`` — the state the goods actually left, which is what the
  tax flavour has to agree with.

* **The company's TIN/CST/PAN are per-location, not per-company.** They come
  from ``OLCT`` keyed on the *line's* ``LocCode``, so the same company prints a
  different CST number on a Sonipat receipt and a Bhakharpur one.

* **Tax rows are built, not read.** The procedure's own ``cstvat`` looks at
  ``PDN4.staType IN (1, 4)`` — VAT and CST, both dead under GST — and answers
  NULL on every current document. The printed "IGST@18.00 %" line is Crystal
  grouping ``PDN4`` by tax component, so that is what this does: the label is
  ``OSTT.Name`` with SAP's ``sys_`` prefix stripped, the rate is the row's own,
  and the amount is the component's summed ``TaxSum``. Intra-state receipts
  therefore print a CGST row and an SGST row, as they should.

* **Payment terms tolerate a term-less document.** The procedure joins ``OCTG``
  on ``GroupNum = T0."GroupNum" OR T0."GroupNum" IS NULL``; SAP writes ``-1``
  rather than NULL for "none", and ``-1`` is a real row ("ADVANCE/CASH/0 DAYS").
"""

import logging
from decimal import Decimal
from typing import Optional

from hdbcli import dbapi

from .ar_invoice_print_reader import FSSAI_BY_BRANCH, FSSAI_DEFAULT
from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# SAP prefixes the GST components' ``OSTT`` names with ``sys_``; the printed
# sheet does not ("sys_IGST" -> "IGST@18.00 %").
TAX_NAME_PREFIX = "sys_"


class HanaGRPOPrintReader:
    """One posted GRPO, shaped for the Goods Receipt Note print."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def grpo_print(self, doc_entry: int) -> Optional[dict]:
        """Everything the printed note needs for one ``OPDN`` document.

        Returns ``None`` when the company has no such receipt — the caller turns
        that into a 404 rather than printing an empty sheet.
        """
        doc_entry = int(doc_entry)

        header = self._header(doc_entry)
        if not header:
            return None

        lines = self._lines(doc_entry)
        header["company"]["fssai_no"] = FSSAI_BY_BRANCH.get(
            header["branch_id"], FSSAI_DEFAULT
        )
        # The company's tax registrations hang off the receiving location, which
        # is a line-level field; every line of one receipt shares it.
        header["company"].update(lines[0]["_location"] if lines else self._blank_location())

        payload = {
            **header,
            "lines": [
                {k: v for k, v in line.items() if not k.startswith("_")} for line in lines
            ],
            "totals": self._totals(doc_entry, header, lines),
        }
        for private in ("_doc_total", "_vat_sum", "_disc_sum", "_round_dif", "_expenses"):
            payload.pop(private, None)
        return payload

    # ------------------------------------------------------------------
    # header
    # ------------------------------------------------------------------

    def _header(self, doc_entry: int) -> Optional[dict]:
        rows = self._query(
            """
            SELECT
                H."DocEntry", H."DocNum", H."DocDate", H."DocDueDate",
                H."CreateDate", H."BPLId",
                IFNULL(H."CardCode", ''), IFNULL(H."CardName", ''),
                IFNULL(H."Address", ''), IFNULL(H."Address2", ''),
                IFNULL(H."NumAtCard", ''), IFNULL(H."Comments", ''),
                IFNULL(H."DocCur", 'INR'),
                H."DocTotal", H."DocTotalFC", H."VatSum", H."DiscSum",
                H."RoundDif", IFNULL(H."TotalExpns", 0),
                (SELECT A."CompnyName" FROM "{schema}"."OADM" A),
                (SELECT IFNULL(A."Phone1", '') FROM "{schema}"."OADM" A),
                (SELECT T."PymntGroup" FROM "{schema}"."OCTG" T
                  WHERE T."GroupNum" = IFNULL(H."GroupNum", -1)),
                IFNULL(P."Name", ''), IFNULL(P."Cellolar", ''), IFNULL(P."E_MailL", ''),
                IFNULL(S."GSTRegnNo", ''),
                -- The PO the receipt was copied from. Every line of a GRPO
                -- raised here shares one PO, but SAP allows several, so the
                -- header takes the distinct set the lines actually name.
                (SELECT STRING_AGG(R."BaseRef", ', ' ORDER BY R."BaseRef")
                   FROM (SELECT DISTINCT L."BaseRef" FROM "{schema}"."PDN1" L
                          WHERE L."DocEntry" = H."DocEntry"
                            AND IFNULL(L."BaseRef", '') != '') R)
            FROM "{schema}"."OPDN" H
            LEFT JOIN "{schema}"."OCPR" P
                   ON P."CardCode" = H."CardCode" AND P."CntctCode" = H."CntctCode"
            LEFT JOIN "{schema}"."CRD1" S
                   ON S."CardCode" = H."CardCode" AND S."Address" = H."ShipToCode"
                  AND S."AdresType" = 'S'
            WHERE H."DocEntry" = ?
            """,
            (doc_entry,),
        )
        if not rows:
            return None

        (
            entry, doc_num, doc_date, due_date, created_on, branch_id,
            card_code, card_name, vendor_address, company_address,
            num_at_card, comments, doc_cur,
            doc_total, doc_total_fc, vat_sum, disc_sum, round_dif, expenses,
            company_name, company_phone, payment_terms,
            contact_name, contact_mobile, contact_email, ship_from_gstin,
            po_ref_no,
        ) = rows[0]

        return {
            "doc_entry": int(entry),
            "doc_num": int(doc_num) if doc_num is not None else None,
            "doc_date": self._date(doc_date),
            "due_date": self._date(due_date),
            "created_on": self._date(created_on),
            "branch_id": int(branch_id) if branch_id is not None else None,
            "currency": doc_cur or "INR",
            "po_ref_no": po_ref_no or "",
            # No field behind the label; see the module docstring.
            "po_ref_date": None,
            "supplier_ref_no": num_at_card or "",
            "payment_terms": payment_terms or "",
            "remarks": comments or "",
            # The sheet's letterhead is the company name and phone alone. SAP
            # reads the receiving plant's address into the layout too
            # (``OPDN.Address2``, ``company_address`` here) and prints none of
            # it, so it is not carried onto the payload either.
            "company": {
                "name": company_name or "",
                "phone": company_phone or "",
            },
            "vendor": {
                "code": card_code or "",
                "name": card_name or "",
                "address_lines": self._address_lines(vendor_address),
                "contact_person": contact_name or "",
                "contact_no": contact_mobile or "",
                "email": contact_email or "",
                "gst_no": ship_from_gstin or "",
                # Constants in the layout — the labels print, the values do not.
                "tin_no": "",
                "cst_no": "",
                "pan_no": "",
            },
            "_doc_total": self._num(doc_total_fc) or self._num(doc_total),
            "_vat_sum": self._num(vat_sum),
            "_disc_sum": self._num(disc_sum),
            "_round_dif": self._num(round_dif),
            "_expenses": self._num(expenses),
        }

    @staticmethod
    def _address_lines(address: str) -> list:
        """SAP stores a document address as one field with ``\\r`` separators.

        The last line is the two-letter country code ("IN"), which the layout
        suppresses — the printed original has room for it below the city line
        and prints the city line last. Dropped here so the two sheets agree.
        """
        if not address:
            return []
        lines = [part.strip() for part in str(address).split("\r") if part.strip()]
        if lines and len(lines[-1]) == 2 and lines[-1].isalpha():
            lines.pop()
        return lines

    @staticmethod
    def _blank_location() -> dict:
        return {"tin_no": "", "cst_no": "", "pan_no": ""}

    # ------------------------------------------------------------------
    # lines
    # ------------------------------------------------------------------

    def _lines(self, doc_entry: int) -> list:
        rows = self._query(
            """
            SELECT
                L."LineNum", L."ItemCode", IFNULL(L."Dscription", ''),
                L."Quantity", IFNULL(L."WhsCode", ''),
                IFNULL(I."SalUnitMsr", IFNULL(L."unitMsr", '')),
                L."Price", IFNULL(L."BaseRef", ''), O."Price",
                L."LineTotal", L."TotalFrgn",
                IFNULL(C."TinNo", ''), IFNULL(C."CstNo", ''), IFNULL(C."PanNo", '')
            FROM "{schema}"."PDN1" L
            LEFT JOIN "{schema}"."OITM" I ON I."ItemCode" = L."ItemCode"
            LEFT JOIN "{schema}"."OLCT" C ON C."Code" = L."LocCode"
            LEFT JOIN "{schema}"."POR1" O
                   ON O."DocEntry" = L."BaseEntry" AND O."ItemCode" = L."ItemCode"
                  AND O."LineNum" = L."BaseLine"
            WHERE L."DocEntry" = ?
            ORDER BY L."LineNum"
            """,
            (doc_entry,),
        )

        lines = []
        for (
            line_num, item_code, description, quantity, warehouse, uom,
            price, po_no, po_price, line_total, total_frgn,
            tin_no, cst_no, pan_no,
        ) in rows:
            # The layout takes the foreign-currency total when there is one and
            # the local total otherwise — one column, either currency.
            amount = self._num(total_frgn) or self._num(line_total)
            lines.append({
                "sno": int(line_num) + 1,
                "item_code": item_code or "",
                "description": description or "",
                "warehouse_code": warehouse or "",
                "quantity": self._out(self._num(quantity)),
                "uom": uom or "",
                "po_no": po_no or "",
                "po_price": self._out(self._num(po_price)),
                # The layout's stub, not a price; see the module docstring.
                "top3_price": "9",
                "price": self._out(self._num(price)),
                "amount": self._out(amount),
                "_quantity": self._num(quantity),
                "_amount": amount,
                "_location": {
                    "tin_no": tin_no or "",
                    "cst_no": cst_no or "",
                    "pan_no": pan_no or "",
                },
            })
        return lines

    # ------------------------------------------------------------------
    # totals
    # ------------------------------------------------------------------

    def _totals(self, doc_entry: int, header: dict, lines: list) -> dict:
        sub_total = sum((line["_amount"] for line in lines), Decimal(0))
        round_dif = header["_round_dif"]
        return {
            "total_qty": self._out(sum((line["_quantity"] for line in lines), Decimal(0))),
            "sub_total": self._out(sub_total),
            "discount": self._out(header["_disc_sum"]),
            "taxes": self._tax_rows(doc_entry),
            # SAP prints this row whether or not the receipt carries charges: on
            # a receipt with none the label is blank and the amount 0.00.
            "expenses": {
                "label": self._expense_label(doc_entry),
                "amount": self._out(header["_expenses"]),
            },
            "round_off": (
                {"label": "Short & Excess", "amount": self._out(round_dif)}
                if round_dif
                else None
            ),
            # The document's own total, not a re-addition of the rows above: it
            # is what SAP prints and what the vendor's copy will say.
            "grand_total": self._out(header["_doc_total"]),
        }

    def _tax_rows(self, doc_entry: int) -> list:
        """One row per GST component on the receipt, as the layout groups them."""
        rows = self._query(
            """
            SELECT T."staType", MAX(T."TaxRate"), SUM(T."TaxSum"),
                   MAX(IFNULL(N."Name", ''))
            FROM "{schema}"."PDN4" T
            LEFT JOIN "{schema}"."OSTT" N ON N."AbsId" = T."staType"
            WHERE T."DocEntry" = ? AND IFNULL(T."TaxSum", 0) != 0
            GROUP BY T."staType"
            ORDER BY T."staType" DESC
            """,
            (doc_entry,),
        )

        taxes = []
        for sta_type, rate, tax_sum, name in rows:
            component = (name or "").removeprefix(TAX_NAME_PREFIX) or f"Tax {sta_type}"
            taxes.append({
                "label": f"{component}@{self._num(rate):.2f} %",
                "amount": self._out(self._num(tax_sum)),
            })
        return taxes

    def _expense_label(self, doc_entry: int) -> str:
        rows = self._query(
            """
            SELECT IFNULL(N."ExpnsName", '')
            FROM "{schema}"."PDN3" E
            LEFT JOIN "{schema}"."OEXD" N ON N."ExpnsCode" = E."ExpnsCode"
            WHERE E."DocEntry" = ? AND IFNULL(E."LineTotal", 0) != 0
            ORDER BY E."LineNum"
            """,
            (doc_entry,),
        )
        return ", ".join(name for (name,) in rows if name)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _num(value) -> Decimal:
        if value is None:
            return Decimal(0)
        try:
            return Decimal(str(value))
        except Exception:
            return Decimal(0)

    @staticmethod
    def _out(value: Decimal) -> str:
        """Decimals cross the wire as strings — JSON floats would round money."""
        return str(value)

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
            logger.error("SAP HANA connection failed while reading a GRPO print: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA GRPO print query failed: %s", e)
            raise SAPDataError("Failed to read the goods receipt from SAP.") from e
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
