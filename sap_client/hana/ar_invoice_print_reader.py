"""Everything SAP's own A/R invoice layout prints, read straight from HANA.

The warehouse raises invoices in this app but the printed TAX INVOICE has always
come out of SAP, so the sheet the floor and the customer know is SAP's Crystal
layout. This reader reproduces that layout's data source rather than inventing
one: the fields below were taken from the stored procedure the layout actually
runs, ``CRYSTAL_AR_INVOICE_ITEMS`` (one per company schema), read out of
``SYS.PROCEDURES`` and mirrored here. Where this file looks eccentric, the
procedure is why.

The parts of that mapping worth knowing before changing anything:

* **The HSN code is the item's, not the line's.** ``INV1.HsnEntry`` exists and is
  usually the same number, but the layout reads ``OITM.ChapterID -> OCHP.ChapterID``
  and so does this. ``OSAC`` is the *service* code table and answers with an
  unrelated code for the same key — reading it is a silent wrong answer, not an
  error.

* **The company letterhead is per-branch data, not a constant.** The address, GST
  number and PAN come from ``OLCT`` — the *location* of the invoice's own lines
  (``INV1.LocCode``) — which is why the same company prints a different address
  on a Delhi bill and a Ganaur one. Only the FSSAI licence is hardcoded in the
  layout, keyed by branch and warehouse, and that table is reproduced verbatim.

* **Bill-to and ship-to are formatted differently on purpose.** Both are the same
  comma-joined run of ``CRD1`` address parts, but the layout prints the bill-to
  with the raw state and country *codes* ("SONIPAT, HR, IN") and the ship-to with
  their full names plus the postcode ("SONIPAT, HARYANA, 131028, India"). It
  looks like an inconsistency and it is one, but it is SAP's, and a sheet that
  "fixes" it stops matching the bill the customer already has.

* **Box and loose come from SalFactor2/SalFactor3, and 1 means loose.** An item
  with ``SalFactor2 = 1`` is not transacted in boxes at all, so it prints
  "0 Box 500.00 PCS" rather than 500 boxes; see ``gate_core.services.box_packing``
  for the same rule and the CSD exception that applies to scanning but not here.

* **Vehicle number and e-way bill are literally ``'0'``.** The procedure selects
  the constant, so every SAP-printed bill carries a zero in both slots whatever
  the dispatch actually was. Reproduced, so the two sheets agree.

* **Litres and gross weight are the app's formulas, not the procedure's.** The
  procedure's ``Gross_Weights`` is ``Quantity * U_Gross_Weight``, which is the
  weight of a *box* multiplied by a count of *pieces* — on the sample invoice it
  answers 98.89 kg where the printed sheet says 4.94. The Product Category panel
  is a Crystal subreport with its own source; its printed numbers match the
  formulas ``dispatch_plans.hana_reader`` already uses (litres = qty x SalPackUn
  gated on ``U_IsLitre``, weight = qty x U_Gross_Weight / SalFactor2), so those
  are what this reader computes.
"""

import logging
from decimal import Decimal
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# ``INV4.staType`` — SAP's internal tax-type ids for the Indian GST components.
CGST_TYPES = ("-100",)
SGST_TYPES = ("-110", "-150")  # -150 is UTGST, which prints in the SGST slot
IGST_TYPES = ("-120",)

# The one genuinely hardcoded table in the Crystal layout: the FSSAI licence
# printed in the letterhead, keyed by SAP branch and (for one warehouse) the
# warehouse the goods left from.
FSSAI_BY_BRANCH = {1: "13322999001306", 2: "10015064000541", 3: "12123999000082"}
FSSAI_DEFAULT = "10014011001626"
FSSAI_BH_LR = "10824999000237"


class HanaARInvoicePrintReader:
    """One posted A/R invoice, shaped for the TAX INVOICE print."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def invoice_print(self, doc_entry: int) -> Optional[dict]:
        """Everything the printed bill needs for one ``OINV`` document.

        Returns ``None`` when the company has no such invoice — the caller turns
        that into a 404 rather than printing an empty sheet.
        """
        doc_entry = int(doc_entry)

        header = self._header(doc_entry)
        if not header:
            return None

        lines = self._lines(doc_entry)
        taxes = self._tax_lines(doc_entry)
        header.update(self._einvoice(doc_entry))

        first_warehouse = lines[0]["warehouse_code"] if lines else ""
        header["company"]["fssai_no"] = self._fssai(header["branch_id"], first_warehouse)

        payload = {
            **header,
            "lines": lines,
            "tax_summary": self._tax_summary(taxes),
            "hsn_summary": self._hsn_summary(lines, taxes),
            "category_summary": self._category_summary(lines),
            "totals": self._totals(header, lines),
        }
        # The raw document money stays inside this reader; the sheet reads the
        # `totals` block, which is where the arithmetic has already happened.
        for private in ("_doc_total", "_vat_sum", "_disc_sum", "_round_dif", "_wt_sum"):
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
                H."U_Dipatch_Date", H."CardCode", H."CardName",
                IFNULL(H."NumAtCard", ''), IFNULL(H."Comments", ''),
                H."DocTotal", H."VatSum", H."DiscSum", H."RoundDif",
                IFNULL(H."WTSum", 0), IFNULL(H."DocCur", 'INR'), H."BPLId",
                IFNULL(C."U_Main_Group", ''), IFNULL(C."U_Fssai", ''),
                (SELECT G."GroupName" FROM "{schema}"."OCRG" G
                  WHERE G."GroupCode" = 113),
                (SELECT T."PymntGroup" FROM "{schema}"."OCTG" T
                  WHERE T."GroupNum" = C."GroupNum"),
                (SELECT MIN(P."Name") FROM "{schema}"."OCPR" P
                  WHERE P."CardCode" = H."CardCode"),
                (SELECT MIN(P."Cellolar") FROM "{schema}"."OCPR" P
                  WHERE P."CardCode" = H."CardCode"),
                (SELECT MIN(P."E_MailL") FROM "{schema}"."OCPR" P
                  WHERE P."CardCode" = H."CardCode"),
                L."GSTRegnNo", L."PanNo",
                -- Built and title-cased in SQL because the layout does exactly
                -- this: INITCAP over the whole run. Doing it in Python would
                -- mean guessing HANA's word rules, and the difference shows —
                -- OCST holds "HARYANA" where the printed letterhead says
                -- "Haryana".
                INITCAP(
                    IFNULL(L."Street", '') || ' ' || IFNULL(L."Block", '') || ' ' ||
                    IFNULL(TO_NVARCHAR(L."Building"), '') || ' ' ||
                    IFNULL(L."City", '') || ' - ' ||
                    IFNULL((SELECT S."Name" FROM "{schema}"."OCST" S
                             WHERE S."Code" = L."State"
                               AND S."Country" = L."Country"), '') || '-' ||
                    IFNULL((SELECT Y."Name" FROM "{schema}"."OCRY" Y
                             WHERE Y."Code" = L."Country"), '') || '-' ||
                    IFNULL(L."ZipCode", '')
                ),
                (SELECT S."Name" FROM "{schema}"."OCST" S
                  WHERE S."Code" = L."State" AND S."Country" = L."Country"),
                (SELECT S."GSTCode" FROM "{schema}"."OCST" S
                  WHERE S."Code" = L."State" AND S."Country" = L."Country"),
                H."PayToCode", H."ShipToCode"
            FROM "{schema}"."OINV" H
            JOIN "{schema}"."OCRD" C ON C."CardCode" = H."CardCode"
            LEFT JOIN "{schema}"."OLCT" L ON L."Code" = (
                SELECT MIN(X."LocCode") FROM "{schema}"."INV1" X
                 WHERE X."DocEntry" = H."DocEntry"
            )
            WHERE H."DocEntry" = ?
            """,
            (doc_entry,),
        )
        if not rows:
            return None

        (
            entry, doc_num, doc_date, due_date, dispatch_date,
            card_code, card_name, num_at_card, comments,
            doc_total, vat_sum, disc_sum, round_dif, wt_sum, doc_cur, branch_id,
            trade, customer_fssai, state_group, payment_terms,
            contact_name, contact_mobile, contact_email,
            company_gstin, company_pan, company_address,
            state_name, state_gst_code,
            pay_to_code, ship_to_code,
        ) = rows[0]

        # Both sides print the document's own CardName. The layout swaps in the
        # address *code* instead for the handful of partners flagged
        # ``U_AddressIdPrint = 'Y'``; none carry the flag today, and a bill that
        # named an address code where every other bill names the customer would
        # read as the wrong customer.
        bill_to = {**self._party(card_code, pay_to_code, "B"), "name": card_name or ""}
        ship_to = {**self._party(card_code, ship_to_code, "S"), "name": card_name or ""}

        return {
            "doc_entry": int(entry),
            "doc_num": int(doc_num) if doc_num is not None else None,
            "doc_date": self._date(doc_date),
            "due_date": self._date(due_date),
            "dispatch_date": self._date(dispatch_date),
            "customer_code": card_code or "",
            "customer_name": card_name or "",
            "customer_ref": num_at_card or "",
            "customer_fssai": customer_fssai or "",
            "comments": comments or "",
            "currency": doc_cur or "INR",
            "branch_id": int(branch_id) if branch_id is not None else None,
            # The strip above the barcode: "CUSTA000025 - CASH SALE - PAN INDIA".
            "trade": trade or "",
            "state_group": state_group or "",
            "payment_terms": payment_terms or "",
            "contact_name": contact_name or "",
            "contact_mobile": contact_mobile or "",
            "contact_email": contact_email or "",
            # Both are the layout's own constants, not fields on the document.
            "vehicle_no": "0",
            "way_bill_no": "0",
            "reverse_charge": "No",
            "place_of_supply": ship_to["state_name"],
            "company": {
                "gstin": company_gstin or "",
                "pan": company_pan or "",
                "address": company_address or "",
                "state_name": state_name or "",
                "state_code": state_gst_code or "",
            },
            "bill_to": bill_to,
            "ship_to": ship_to,
            "_doc_total": self._num(doc_total),
            "_vat_sum": self._num(vat_sum),
            "_disc_sum": self._num(disc_sum),
            "_round_dif": self._num(round_dif),
            "_wt_sum": self._num(wt_sum),
        }

    def _party(self, card_code: str, address_code: str, addr_type: str) -> dict:
        """One side of the bill from ``CRD1``, formatted as the layout formats it.

        ``addr_type`` is ``'B'`` (bill-to, address = ``PayToCode``) or ``'S'``
        (ship-to, address = ``ShipToCode``). The two are joined from the same
        parts but the ship-to spells the state and country out and carries the
        postcode; see the module docstring.
        """
        blank = {"name": "", "address": "", "gstin": "", "state_name": "", "state_code": ""}
        if not address_code:
            return blank

        rows = self._query(
            """
            SELECT
                A."Building", A."StreetNo", A."Address2", A."Address3",
                A."Street", A."Block", A."City", A."State", A."ZipCode",
                A."Country", A."GSTRegnNo",
                (SELECT S."Name" FROM "{schema}"."OCST" S
                  WHERE S."Code" = A."State" AND S."Country" = A."Country"),
                (SELECT S."GSTCode" FROM "{schema}"."OCST" S
                  WHERE S."Code" = A."State" AND S."Country" = A."Country"),
                (SELECT Y."Name" FROM "{schema}"."OCRY" Y WHERE Y."Code" = A."Country")
            FROM "{schema}"."CRD1" A
            WHERE A."CardCode" = ? AND A."AdresType" = ? AND A."Address" = ?
            """,
            (card_code, addr_type, address_code),
        )
        if not rows:
            return blank

        (
            building, street_no, address2, address3, street, block, city,
            state_code, zip_code, country_code, gstin,
            state_name, state_gst_code, country_name,
        ) = rows[0]

        # SAP appends ", " after every part that is not NULL — an empty-string
        # part still contributes its separator, which is why a bill with no
        # street prints a leading comma. Kept, so the two sheets read alike.
        parts = [building, street_no, address2, address3, street, block, city]
        if addr_type == "S":
            parts += [state_name, zip_code, country_name]
        else:
            parts += [state_code, country_code]

        return {
            "name": "",  # filled by the caller from CardName
            "address": ", ".join("" if p is None else str(p) for p in parts if p is not None),
            "gstin": gstin or "",
            "state_name": state_name or "",
            "state_code": state_gst_code or "",
        }

    @staticmethod
    def _fssai(branch_id: Optional[int], warehouse_code: str) -> str:
        if branch_id == 2:
            return FSSAI_BH_LR if warehouse_code == "BH-LR" else FSSAI_BY_BRANCH[2]
        return FSSAI_BY_BRANCH.get(branch_id, FSSAI_DEFAULT)

    # ------------------------------------------------------------------
    # lines
    # ------------------------------------------------------------------

    def _lines(self, doc_entry: int) -> list[dict]:
        """The item rows, with the batch, HSN and box/loose split SAP prints.

        ``TreeType != 'I'`` drops the component rows of a sales BOM: SAP prints
        the parent line and would otherwise double the sheet's quantities.
        """
        rows = self._query(
            """
            SELECT
                L."LineNum", L."ItemCode", IFNULL(L."Dscription", ''),
                L."Quantity", L."Price", L."PriceBefDi",
                IFNULL(L."DiscPrcnt", 0), L."LineTotal",
                IFNULL(L."WhsCode", ''), IFNULL(L."unitMsr", ''),
                IFNULL(I."SalFactor2", 1), IFNULL(I."SalFactor3", 1),
                IFNULL(I."SalPackMsr", IFNULL(I."SalUnitMsr", '')),
                IFNULL(I."SalPackUn", 0), IFNULL(I."U_IsLitre", 'N'),
                IFNULL(I."U_Gross_Weight", 0), IFNULL(I."U_Sub_Group", ''),
                (SELECT P."ChapterID" FROM "{schema}"."OCHP" P
                  WHERE P."AbsEntry" = I."ChapterID"),
                IFNULL((
                    SELECT MIN(B."BatchNum") FROM "{schema}"."IBT1" B
                     WHERE B."BaseType" = '13' AND B."BaseEntry" = L."DocEntry"
                       AND B."ItemCode" = L."ItemCode"
                       AND B."BaseLinNum" = L."LineNum"
                ), '')
            FROM "{schema}"."INV1" L
            JOIN "{schema}"."OITM" I ON I."ItemCode" = L."ItemCode"
            WHERE L."DocEntry" = ? AND IFNULL(L."TreeType", '') != 'I'
            ORDER BY L."LineNum"
            """,
            (doc_entry,),
        )

        lines = []
        for (
            line_num, item_code, description,
            quantity, price, price_bef_di, disc_pct, line_total,
            warehouse_code, uom,
            sal_factor2, sal_factor3, pack_msr, pack_un, is_litre,
            gross_weight, sub_group, hsn, batch_no,
        ) in rows:
            qty = self._num(quantity)
            factor2 = self._num(sal_factor2) or Decimal(1)
            factor3 = self._num(sal_factor3) or Decimal(1)

            boxes, loose = self._split(qty, factor2, factor3, pack_msr)
            lines.append({
                "line_num": int(line_num),
                "item_code": item_code or "",
                "description": description or "",
                "batch_no": batch_no or "",
                "hsn": hsn or "",
                "warehouse_code": warehouse_code or "",
                "quantity": self._out(qty * factor3 if factor3 > 1 else qty),
                "boxes": boxes,
                "loose_qty": self._out(loose),
                "loose_uom": (pack_msr or uom or "").strip(),
                "rate_per_bottle": self._out(self._num(price_bef_di)),
                "discount_pct": self._out(self._num(disc_pct)),
                "net_rate_per_bottle": self._out(self._num(price)),
                "taxable_value": self._out(self._num(line_total)),
                "category": (sub_group or "").strip(),
                # Litres bill by SalPackUn only when the item is flagged as a
                # liquid; without the gate a line of 100,000 preforms would
                # report 100,000 litres.
                "litres": self._out(
                    qty * self._num(pack_un)
                    if str(is_litre or "").upper() == "Y"
                    else Decimal(0)
                ),
                "gross_weight": self._out(
                    qty * self._num(gross_weight) / factor2 if factor2 else Decimal(0)
                ),
            })
        return lines

    @staticmethod
    def _split(qty: Decimal, factor2: Decimal, factor3: Decimal, pack_msr: str):
        """SAP's ``BoxInt`` / ``LooseQty``, transcribed branch for branch.

        The two are computed independently in the procedure and do not share a
        condition, so they are written out separately here rather than folded
        into one if/else that would look tidier and answer differently.
        """
        if factor3 > 1:
            boxes = int(qty)
        elif factor2 == 1:
            # Not transacted in boxes at all: the line ships loose, per piece.
            boxes = 0
        else:
            boxes = int(qty / factor2)

        if factor2 != 1:
            loose = qty - Decimal(int(qty / factor2)) * factor2
        elif (pack_msr or "") == "Drum" or factor3 != 1:
            loose = Decimal(0)
        else:
            loose = qty

        return boxes, loose

    # ------------------------------------------------------------------
    # taxes
    # ------------------------------------------------------------------

    def _tax_lines(self, doc_entry: int) -> list[dict]:
        rows = self._query(
            """
            SELECT T."LineNum", T."staType", IFNULL(T."StaCode", ''),
                   IFNULL(T."TaxRate", 0), IFNULL(T."TaxSum", 0),
                   T."RelateType"
            FROM "{schema}"."INV4" T
            WHERE T."DocEntry" = ? AND T."RelateType" IN (1, 3)
            ORDER BY T."LineNum", T."LineSeq"
            """,
            (doc_entry,),
        )
        return [
            {
                "line_num": int(line_num) if line_num is not None else -1,
                "sta_type": str(sta_type),
                "code": code or "",
                "rate": self._num(rate),
                "amount": self._num(amount),
                # 1 = tax on an item line, 3 = tax on a freight/expense line.
                # Both belong in the totals; only 1 can be attributed to an item.
                "on_item_line": int(relate_type) == 1,
            }
            for line_num, sta_type, code, rate, amount, relate_type in rows
        ]

    def _tax_summary(self, taxes: list[dict]) -> list[dict]:
        """One row per GST component, labelled the way the sheet labels it.

        The label is the SAP tax code with ``.00 %`` stuck on the end. That reads
        correctly for a whole-number rate (``IGST@18`` -> "IGST@18.00 %") and
        visibly wrongly for a fractional one (``CGST@2.5`` -> "CGST@2.5.00 %") —
        and "CGST@2.5.00 %" is exactly what SAP prints. It is SAP's blemish, not
        one to correct here: a bill whose tax line reads differently from the
        copy the customer already holds is a bill somebody has to reconcile.
        """
        buckets: dict[str, dict] = {}
        order: list[str] = []
        for tax in taxes:
            if tax["sta_type"] not in CGST_TYPES + SGST_TYPES + IGST_TYPES:
                continue
            key = f"{tax['code']}|{tax['rate']}"
            if key not in buckets:
                buckets[key] = {
                    "label": f"{tax['code']}.00 %" if tax["code"] else "",
                    "amount": Decimal(0),
                }
                order.append(key)
            buckets[key]["amount"] += tax["amount"]
        return [
            {"label": buckets[key]["label"], "amount": self._out(buckets[key]["amount"])}
            for key in order
        ]

    def _hsn_summary(self, lines: list[dict], taxes: list[dict]) -> list[dict]:
        """Taxable value and tax per HSN code — the small table under the items."""
        tax_by_line: dict[int, Decimal] = {}
        rate_by_line: dict[int, Decimal] = {}
        for tax in taxes:
            if not tax["on_item_line"]:
                continue
            if tax["sta_type"] not in CGST_TYPES + SGST_TYPES + IGST_TYPES:
                continue
            tax_by_line[tax["line_num"]] = tax_by_line.get(tax["line_num"], Decimal(0)) + tax["amount"]
            rate_by_line[tax["line_num"]] = rate_by_line.get(tax["line_num"], Decimal(0)) + tax["rate"]

        buckets: dict[str, dict] = {}
        order: list[str] = []
        for line in lines:
            key = line["hsn"]
            if key not in buckets:
                buckets[key] = {
                    "hsn": key,
                    "taxable_value": Decimal(0),
                    "tax_rate": rate_by_line.get(line["line_num"], Decimal(0)),
                    "total_tax": Decimal(0),
                }
                order.append(key)
            buckets[key]["taxable_value"] += Decimal(line["taxable_value"])
            buckets[key]["total_tax"] += tax_by_line.get(line["line_num"], Decimal(0))

        return [
            {
                "hsn": buckets[key]["hsn"],
                "taxable_value": self._out(buckets[key]["taxable_value"]),
                "tax_rate": self._out(buckets[key]["tax_rate"]),
                "total_tax": self._out(buckets[key]["total_tax"]),
            }
            for key in order
        ]

    @staticmethod
    def _category_summary(lines: list[dict]) -> list[dict]:
        """The Product Category panel: litres and gross weight per item variety."""
        buckets: dict[str, dict] = {}
        order: list[str] = []
        for line in lines:
            key = line["category"]
            if not key:
                continue
            if key not in buckets:
                buckets[key] = {"category": key, "litres": Decimal(0), "gross_weight": Decimal(0)}
                order.append(key)
            buckets[key]["litres"] += Decimal(line["litres"])
            buckets[key]["gross_weight"] += Decimal(line["gross_weight"])
        return [
            {
                "category": buckets[key]["category"],
                "litres": str(buckets[key]["litres"]),
                "gross_weight": str(buckets[key]["gross_weight"]),
            }
            for key in order
        ]

    def _totals(self, header: dict, lines: list[dict]) -> dict:
        taxable = sum((Decimal(line["taxable_value"]) for line in lines), Decimal(0))
        boxes = sum(line["boxes"] for line in lines)
        loose = sum((Decimal(line["loose_qty"]) for line in lines), Decimal(0))
        quantity = sum((Decimal(line["quantity"]) for line in lines), Decimal(0))

        # SAP's DocTotal already carries the withholding, so the "Total" line is
        # the document net of it and the grand total is the document itself.
        tcs = header["_wt_sum"]
        doc_total = header["_doc_total"]

        return {
            "taxable_value": self._out(taxable),
            "discount": self._out(header["_disc_sum"]),
            "round_off": self._out(header["_round_dif"]),
            "total": self._out(doc_total - tcs),
            "tcs": self._out(tcs),
            "grand_total": self._out(doc_total),
            "boxes": boxes,
            "loose_qty": self._out(loose),
            "loose_uom": next((line["loose_uom"] for line in lines if line["loose_uom"]), ""),
            "quantity": self._out(quantity),
            "litres": self._out(sum((Decimal(l["litres"]) for l in lines), Decimal(0))),
            "gross_weight": self._out(sum((Decimal(l["gross_weight"]) for l in lines), Decimal(0))),
        }

    # ------------------------------------------------------------------
    # e-invoice
    # ------------------------------------------------------------------

    def _einvoice(self, doc_entry: int) -> dict:
        """IRN / acknowledgement, from the e-invoicing add-on's own UDT.

        ``@UTL_MDEXTH`` belongs to a third-party add-on, so a company that does
        not have it installed simply has no table. That is a blank IRN block on
        the sheet — exactly what SAP prints for a bill that was never e-invoiced
        — and not a reason to fail the whole print.
        """
        empty = {"irn": "", "ack_no": "", "ack_date": None}
        try:
            rows = self._query(
                """
                SELECT IFNULL(A."U_UTL_IRN", ''), IFNULL(A."U_UTL_AckNo", ''),
                       A."U_UTL_IRNGENDT"
                FROM "{schema}"."@UTL_MDEXTH" A
                WHERE A."U_UTL_DocType" = 13 AND A."U_UTL_BaseEntry" = ?
                  AND A."U_UTL_IST" = 'S' AND IFNULL(A."U_UTL_QRPT", '') != ''
                """,
                (doc_entry,),
            )
        except SAPDataError:
            logger.info("No e-invoicing UDT in this company; printing a blank IRN block.")
            return empty
        if not rows:
            return empty
        irn, ack_no, ack_date = rows[0]
        return {"irn": irn or "", "ack_no": ack_no or "", "ack_date": self._date(ack_date)}

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
            logger.error("SAP HANA connection failed while reading an A/R invoice print: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA A/R invoice print query failed: %s", e)
            raise SAPDataError("Failed to read the invoice from SAP.") from e
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
