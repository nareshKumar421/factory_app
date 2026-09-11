"""Everything SAP's own Purchase Order layout prints, read straight from HANA.

Purchase orders are raised in SAP, not here, but the sheet the stores and the
vendor know is SAP's Crystal "Purchase Order". This reader reproduces that
layout's data source rather than inventing one: the fields below were taken from
the stored procedure the layout actually runs, ``CRYSTAL_PURCHASE_ORDER_ITEM``
(one per company schema), read out of ``SYS.PROCEDURES`` and mirrored here, then
checked field by field against a SAP-printed sheet (Beverages PO 826228032,
DocEntry 4131). Where this file looks eccentric, the procedure is why.

The three companies' copies of the procedure have drifted apart, so the places
they disagree are decided here once:

* **The FSSAI licence rule is identical in all three.** The same
  ``BPLId``/``WhsCode`` CASE appears in each, with the same five constants, and
  they are already in the codebase as ``FSSAI_BY_BRANCH``/``FSSAI_BH_LR`` — so
  this reads them from there instead of repeating the CASE.

* **The receiving location's GSTIN has two sources.** Beverages reads the
  document's own snapshot (``POR12.LocGSTN``); Oil and Mart read the location
  master live (``OLCT.GSTRegnNo``). They agree today. The snapshot wins here,
  falling back to the master: it is the number the PO was actually raised under,
  which is what a reprint should say even after somebody edits the master.

* **The vendor's state has three sources.** Beverages and Mart name the state of
  the document's own ship-from address (``CRD1`` on ``ShipToCode``); Oil names
  ``OCRD.State2`` off the vendor master. This takes the address's state, so the
  printed state agrees with the printed GSTIN — they come from one row.

* **Mart title-cases the receiving address** (``INITCAP``) and Oil and Beverages
  do not. Left uppercase, as the reference sheet prints it.

* **Mart alone honours ``OPOR.U_PO_Ship_To``.** That UDF does not exist in the
  other two schemas, and the layout's "Ship To" block prints the location
  address (the procedure's ``ADDRESS``) rather than its ``SHIP TO`` field
  anyway, so nothing here reads it.

The rest of the mapping worth knowing before changing anything:

* **Three labels on the sheet have nothing behind them.** "Packing Slip No." has
  no field in the procedure at all. "Payment Due Date" does have one — the
  procedure computes ``ADD_DAYS(DocDate, OCTG.ExtraDays)`` — but the printed
  original leaves the cell empty where that formula would have put a date, so it
  is deliberately not read. The "Ship To" block's "Contact Person" and
  "Cust. Contact No" are the same case: ``OLCT`` holds a name and a number for
  the reference PO's location and SAP printed neither. A sheet that carried
  values where SAP's carries none is a difference somebody has to reconcile; see
  ``grpo_print_reader`` for the same decision about "Reff. Po Date".

* **"Place of Supply" on a purchase order is the vendor's state**, not ours —
  the procedure's ``Bill To State``, read off the ship-from address. On the
  reference sheet Crystal clips it to the field width ("DADRA AND NAGAR HAVELI
  AND DA"); the full name is carried here and left to wrap.

* **The "Qty (Unit)" column prints no unit.** The procedure selects
  ``POR1.unitMsr`` (and Oil additionally selects ``OITM.SalUnitMsr``), and the
  printed original shows the bare quantity. The unit is carried on each line so
  the sheet can label the column honestly, and the reference layout's own cell
  stays numbers-only.

* **Payment terms tolerate a term-less document.** SAP writes ``-1`` rather than
  NULL for "no payment terms", and ``-1`` is a real ``OCTG`` row, so the days
  resolve to 0 and the sheet reads "0 Days from the date of GRN". Note the
  procedure keys this off the *vendor's* ``OCRD.GroupNum``, not the document's.

* **Tax rows are built, not read.** As on the goods receipt, the printed
  "IGST@18.00 %" line is Crystal grouping ``POR4`` by tax component: the label
  is ``OSTT.Name`` with SAP's ``sys_`` prefix stripped, the rate is the row's
  own and the amount the component's summed ``TaxSum``. Intra-state orders
  therefore print a CGST row and an SGST row, as they should.

* **Freight and round-off rows are derived, not observed.** The reference PO
  carries neither, and Crystal suppresses a zero row, so the original cannot
  show where they sit. ``DocTotal`` is demonstrably
  ``lines + expenses + tax + round-off`` (checked against five Beverages orders
  that do carry charges), so both are carried on the payload and the sheet
  places freight under the label that already names it.

* **The approver is the document's own, not the layout's.** The Beverages sheet
  prints "Bhupinder Singh" in title case where ``OUSR`` stores "BHUPINDER
  SINGH", and the approval chain for the reference PO names somebody else
  entirely — the name is typed into that layout, not read. This reads the real
  approver off ``OWDD``/``WDD1`` as the procedure does. Note that ``OWDD``
  rows are keyed by ``DocEntry`` *and* ``ObjType``: DocEntry 4131 exists as an
  invoice, a transfer and an A/P invoice in the same company, so the draft key
  is what picks out the purchase order's own chain.
"""

import logging
from decimal import Decimal
from typing import Optional

from hdbcli import dbapi

from .ar_invoice_print_reader import FSSAI_BH_LR, FSSAI_BY_BRANCH, FSSAI_DEFAULT
from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# SAP prefixes the GST components' ``OSTT`` names with ``sys_``; the printed
# sheet does not ("sys_IGST" -> "IGST@18.00 %").
TAX_NAME_PREFIX = "sys_"

# ``OPOR.WddStatus`` values that mean the order cleared its approval chain. The
# printed sheet stamps "Approved" off this field; SAP writes 'P' on an approved
# order and 'Y' on one whose approval was generated and accepted.
APPROVED_WDD_STATUSES = {"P", "Y"}


class HanaPOPrintReader:
    """One purchase order, shaped for the Purchase Order print."""

    def __init__(self, context):
        self.company_code = context.company_code
        self.connection = HanaConnection(context.hana)

    def po_print(self, doc_entry: int) -> Optional[dict]:
        """Everything the printed order needs for one ``OPOR`` document.

        Returns ``None`` when the company has no such order — the caller turns
        that into a 404 rather than printing an empty sheet.
        """
        doc_entry = int(doc_entry)

        header = self._header(doc_entry)
        if not header:
            return None

        lines = self._lines(doc_entry, header["_disc_prcnt"])
        # The receiving location is a line-level field (``POR1.LocCode``); every
        # line of one order shares it, and it is what the masthead, the "Ship To"
        # block and the FSSAI licence are all read from.
        location = lines[0]["_location"] if lines else self._blank_location()
        header["company"].update(location)
        header["company"]["fssai_no"] = self._fssai(
            header["branch_id"], lines[0]["_warehouse_code"] if lines else ""
        )
        # Beverages snapshots the location's GSTIN onto the document; Oil and
        # Mart read the master. Prefer the snapshot (see the module docstring).
        header["company"]["gst_no"] = (
            header.pop("_doc_location_gstin", "") or location.get("gst_no", "")
        )

        payload = {
            **header,
            "lines": [
                {k: v for k, v in line.items() if not k.startswith("_")} for line in lines
            ],
            "totals": self._totals(doc_entry, header, lines),
            "hsn_summary": self._hsn_summary(doc_entry, lines),
        }
        for private in (
            "_doc_total", "_disc_sum", "_disc_prcnt", "_round_dif", "_expenses",
        ):
            payload.pop(private, None)
        return payload

    def doc_entry_for_number(self, po_number: str) -> Optional[int]:
        """The ``DocEntry`` behind a printed PO number, or ``None``.

        Every ``POReceipt`` raised since the field was added carries its
        ``sap_doc_entry``, but the older rows do not, and a closed order cannot
        be found through the open-PO reader. Looks at ``DocNum`` — the number
        the operator is reading off the screen — over every order in the
        company, cancelled ones included: a cancelled PO still has a sheet, and
        refusing to print it would leave the operator guessing why.
        """
        po_number = str(po_number or "").strip()
        if not po_number.isdigit():
            return None

        rows = self._query(
            'SELECT H."DocEntry" FROM "{schema}"."OPOR" H WHERE H."DocNum" = ?',
            (int(po_number),),
        )
        return int(rows[0][0]) if rows else None

    # ------------------------------------------------------------------
    # header
    # ------------------------------------------------------------------

    def _header(self, doc_entry: int) -> Optional[dict]:
        rows = self._query(
            """
            SELECT
                H."DocEntry", H."DocNum", H."DocDate", H."DocDueDate",
                H."BPLId", IFNULL(H."BPLName", ''),
                IFNULL(H."CardCode", ''), IFNULL(H."CardName", ''),
                IFNULL(H."NumAtCard", ''), IFNULL(H."Comments", ''),
                IFNULL(H."DocCur", 'INR'),
                H."DocTotal", H."DocTotalFC", H."DiscSum", H."DiscSumFC",
                H."RoundDif", H."RoundDifFC", IFNULL(H."TotalExpns", 0),
                IFNULL(H."DiscPrcnt", 0), IFNULL(H."WddStatus", ''),
                -- The vendor's ship-from address: the GSTIN, state and postal
                -- address the "Bill From" block prints all come from this one
                -- row, so they cannot disagree with each other.
                IFNULL(S."Address2", '') , IFNULL(S."Address3", ''),
                IFNULL(S."Street", ''), IFNULL(S."Block", ''),
                IFNULL(S."City", ''), IFNULL(S."ZipCode", ''),
                IFNULL(S."GSTRegnNo", ''),
                IFNULL(VS."Name", ''), IFNULL(VS."GSTCode", ''),
                IFNULL(V."DflAccount", ''), IFNULL(V."DflSwift", ''),
                IFNULL(V."U_Fssai", ''),
                IFNULL(C."Name", ''), IFNULL(C."Cellolar", ''), IFNULL(C."E_MailL", ''),
                -- Carriers and shipping terms both print blank on a document
                -- that names neither; SAP stores -1 for "none".
                IFNULL(T."TrnspName", ''),
                IFNULL(D."TableDesc", ''),
                -- SAP writes -1 for "no payment terms", and -1 is a real row.
                IFNULL((SELECT G."ExtraDays" FROM "{schema}"."OCTG" G
                         WHERE G."GroupNum" = IFNULL(V."GroupNum", -1)), 0),
                IFNULL(L."LocGSTN", ''),
                IFNULL(L."Vehicle", ''),
                (SELECT MAX(U."U_NAME")
                   FROM "{schema}"."OWDD" W
                   INNER JOIN "{schema}"."WDD1" WL ON WL."WddCode" = W."WddCode"
                   INNER JOIN "{schema}"."OUSR" U ON U."USERID" = WL."UserID"
                  WHERE W."DocEntry" = H."DocEntry"
                    AND W."DraftEntry" = H."draftKey")
            FROM "{schema}"."OPOR" H
            LEFT JOIN "{schema}"."OCRD" V ON V."CardCode" = H."CardCode"
            LEFT JOIN "{schema}"."CRD1" S
                   ON S."CardCode" = H."CardCode" AND S."Address" = H."ShipToCode"
                  AND S."AdresType" = 'S'
            LEFT JOIN "{schema}"."OCST" VS
                   ON VS."Code" = S."State" AND VS."Country" = 'IN'
            LEFT JOIN "{schema}"."OCPR" C
                   ON C."CardCode" = H."CardCode" AND C."CntctCode" = H."CntctCode"
            LEFT JOIN "{schema}"."OSHP" T ON T."TrnspCode" = H."TrnspCode"
            LEFT JOIN "{schema}"."OCTG" PT ON PT."GroupNum" = V."GroupNum"
            LEFT JOIN "{schema}"."OCDC" D ON D."Code" = PT."DiscCode"
            LEFT JOIN "{schema}"."POR12" L ON L."DocEntry" = H."DocEntry"
            WHERE H."DocEntry" = ?
            """,
            (doc_entry,),
        )
        if not rows:
            return None

        (
            entry, doc_num, doc_date, ship_date,
            branch_id, branch_name,
            card_code, card_name, num_at_card, comments, doc_cur,
            doc_total, doc_total_fc, disc_sum, disc_sum_fc,
            round_dif, round_dif_fc, expenses, disc_prcnt, wdd_status,
            v_address2, v_address3, v_street, v_block, v_city, v_zip,
            vendor_gstin, vendor_state, vendor_state_code,
            bank_account, bank_ifsc, vendor_fssai,
            contact_name, contact_mobile, contact_email,
            transport_name, shipping_terms, payment_days,
            doc_location_gstin, vehicle_no, approver,
        ) = rows[0]

        foreign = (doc_cur or "INR") != "INR"
        return {
            "company_code": self.company_code,
            "doc_entry": int(entry),
            "doc_num": int(doc_num) if doc_num is not None else None,
            "doc_date": self._date(doc_date),
            # The layout calls ``DocDueDate`` the ship date, and the procedure
            # agrees ("Delivery Date"). It is not a payment due date.
            "ship_date": self._date(ship_date),
            "branch_id": int(branch_id) if branch_id is not None else None,
            "unit": branch_name or "",
            "currency": doc_cur or "INR",
            "supplier_ref_no": num_at_card or "",
            "remarks": comments or "",
            "transportation_mode": transport_name or "",
            "shipping_terms": shipping_terms or "",
            "payment_terms": f"{int(payment_days or 0)} Days from the date of GRN",
            "vehicle_no": vehicle_no or "",
            # Labels the printed original leaves empty; see the module docstring.
            "payment_due_date": None,
            "packing_slip_no": "",
            "approval": {
                "is_approved": (wdd_status or "") in APPROVED_WDD_STATUSES,
                "approver": approver or "",
            },
            # The receiving location's own registrations and address, filled in
            # from the lines once they are read.
            "company": {
                "state_name": "",
                "state_code": "",
                # Both print blank on the reference sheet even though ``OLCT``
                # holds them; see the module docstring.
                "contact_person": "",
                "contact_no": "",
            },
            "vendor": {
                "code": card_code or "",
                "name": card_name or "",
                "address": self._vendor_address(
                    v_address2, v_address3, v_street, v_block, v_city, v_zip
                ),
                "gst_no": vendor_gstin or "",
                "state_name": vendor_state or "",
                "state_code": vendor_state_code or "",
                "fssai_no": vendor_fssai or "",
                "bank_account": bank_account or "",
                "bank_ifsc": bank_ifsc or "",
                "contact_person": contact_name or "",
                "contact_no": contact_mobile or "",
                "email": contact_email or "",
            },
            # A purchase order's place of supply is the vendor's state, read off
            # the same address row as the GSTIN above.
            "place_of_supply": vendor_state or "",
            "_doc_location_gstin": doc_location_gstin or "",
            "_doc_total": self._num(doc_total_fc if foreign else doc_total),
            "_disc_sum": self._num(disc_sum_fc if foreign else disc_sum),
            "_disc_prcnt": self._num(disc_prcnt),
            "_round_dif": self._num(round_dif_fc if foreign else round_dif),
            "_expenses": self._num(expenses),
        }

    @staticmethod
    def _vendor_address(address2, address3, street, block, city, zip_code) -> str:
        """The ship-from address, glued the way the procedure glues it.

        Two spaces between the parts and " - " before the postcode, with the
        empty parts left as empty — which is why the printed original has wide
        gaps in the middle of the line. Reproduced so the two sheets agree.
        """
        joined = "  ".join(
            str(part or "") for part in (address2, address3, street, block, city)
        )
        return f"{joined} - {zip_code or ''}".strip()

    @staticmethod
    def _fssai(branch_id: Optional[int], warehouse_code: str) -> str:
        """The branch's licence number, by the rule all three procedures share."""
        if branch_id == 2:
            return FSSAI_BH_LR if warehouse_code == "BH-LR" else FSSAI_BY_BRANCH[2]
        return FSSAI_BY_BRANCH.get(branch_id, FSSAI_DEFAULT)

    @staticmethod
    def _blank_location() -> dict:
        return {
            "location_name": "",
            "address": "",
            "gst_no": "",
            "pan_no": "",
        }

    # ------------------------------------------------------------------
    # lines
    # ------------------------------------------------------------------

    def _lines(self, doc_entry: int, header_discount_percent: Decimal) -> list:
        rows = self._query(
            """
            SELECT
                L."LineNum", L."ItemCode", IFNULL(L."Dscription", ''),
                IFNULL(L."U_Remarks", ''),
                L."Quantity", IFNULL(L."unitMsr", ''),
                L."PriceBefDi", L."Price", IFNULL(L."DiscPrcnt", 0),
                L."LineTotal", L."TotalFrgn", IFNULL(L."WhsCode", ''),
                (SELECT MAX(CH."ChapterID") FROM "{schema}"."OCHP" CH
                  WHERE CH."AbsEntry" = I."ChapterID"),
                IFNULL(O."Location", ''), IFNULL(O."GSTRegnNo", ''),
                IFNULL(O."PanNo", ''),
                IFNULL(O."Street", ''), IFNULL(O."Block", ''),
                TO_VARCHAR(IFNULL(O."Building", '')), IFNULL(O."City", ''),
                IFNULL(O."ZipCode", ''), IFNULL(O."State", ''),
                IFNULL(O."Country", ''),
                IFNULL(OS."Name", ''), IFNULL(OS."GSTCode", '')
            FROM "{schema}"."POR1" L
            LEFT JOIN "{schema}"."OITM" I ON I."ItemCode" = L."ItemCode"
            LEFT JOIN "{schema}"."OLCT" O ON O."Code" = L."LocCode"
            LEFT JOIN "{schema}"."OCST" OS
                   ON OS."Code" = O."State" AND OS."Country" = 'IN'
            WHERE L."DocEntry" = ?
            ORDER BY L."LineNum"
            """,
            (doc_entry,),
        )

        # A header-level discount is spread across the lines rather than shown
        # as its own row, so a line's taxable value is its total net of that
        # percentage — which is how the procedure computes "Taxable Value".
        header_disc = Decimal(1) - header_discount_percent / Decimal(100)
        lines = []
        for (
            line_num, item_code, description, details,
            quantity, uom, price_before_disc, price, disc_prcnt,
            line_total, total_frgn, warehouse_code, hsn_code,
            loc_name, loc_gstin, loc_pan,
            loc_street, loc_block, loc_building, loc_city, loc_zip,
            loc_state, loc_country, loc_state_name, loc_state_code,
        ) in rows:
            # One column, either currency — the foreign total when there is one.
            amount = self._num(total_frgn) or self._num(line_total)
            taxable = amount * header_disc
            lines.append({
                "sno": int(line_num) + 1,
                "item_code": item_code or "",
                "description": description or "",
                "details": details or "",
                "hsn_code": hsn_code or "",
                "quantity": self._out(self._num(quantity)),
                "uom": uom or "",
                "rate": self._out(self._num(price_before_disc)),
                "discount_percent": self._out(self._num(disc_prcnt)),
                "net_rate": self._out(self._num(price)),
                "taxable_value": self._out(taxable),
                "_quantity": self._num(quantity),
                "_taxable": taxable,
                "_warehouse_code": warehouse_code or "",
                "_location": {
                    "location_name": loc_name or "",
                    "address": self._location_address(
                        loc_street, loc_block, loc_building, loc_city,
                        loc_zip, loc_state, loc_country,
                    ),
                    "gst_no": loc_gstin or "",
                    "pan_no": loc_pan or "",
                    "state_name": loc_state_name or "",
                    "state_code": loc_state_code or "",
                },
            })
        return lines

    @staticmethod
    def _location_address(street, block, building, city, zip_code, state, country) -> str:
        """The receiving location's address, glued the way the procedure glues it.

        Single spaces, the two-letter state code as stored, and "India" spelled
        out for ``IN`` — everything else drops its country. Mart's copy of the
        procedure wraps this in ``INITCAP``; left uppercase here, as the
        reference sheet prints it.
        """
        parts = [street, block, building, city, zip_code, state]
        joined = " ".join(str(part or "") for part in parts)
        if (country or "") == "IN":
            joined = f"{joined} India"
        return " ".join(joined.split())

    # ------------------------------------------------------------------
    # totals
    # ------------------------------------------------------------------

    def _totals(self, doc_entry: int, header: dict, lines: list) -> dict:
        sub_total = sum((line["_taxable"] for line in lines), Decimal(0))
        round_dif = header["_round_dif"]
        expenses = header["_expenses"]
        return {
            "total_qty": self._out(sum((line["_quantity"] for line in lines), Decimal(0))),
            # The sheet's own label for the line sum: everything before the
            # charges and the discount are applied.
            "amount_before_freight": self._out(sub_total),
            "discount": self._out(header["_disc_sum"]),
            "taxes": self._tax_rows(doc_entry),
            # Suppressed on a sheet with no charges, which is why the reference
            # original cannot show where SAP puts it; see the module docstring.
            "expenses": (
                {"label": self._expense_label(doc_entry), "amount": self._out(expenses)}
                if expenses
                else None
            ),
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
        """One row per GST component on the order, as the layout groups them.

        Deliberately unfiltered on ``RelateType``: freight carries its own tax
        row (``RelateType`` 3, pointing at the ``POR3`` charge rather than an
        item line), and the procedure's own CGST/SGST/IGST fields take only the
        item rows. Leaving the charge's tax out makes the printed figures fail
        to add up to the order's total — checked on Beverages PO 726228019,
        where item tax is 76,503.24, freight tax 2,295.18 and ``VatSum``
        78,798.42.
        """
        rows = self._query(
            """
            SELECT T."staType", MAX(T."TaxRate"), SUM(T."TaxSum"),
                   MAX(IFNULL(N."Name", ''))
            FROM "{schema}"."POR4" T
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
            FROM "{schema}"."POR3" E
            LEFT JOIN "{schema}"."OEXD" N ON N."ExpnsCode" = E."ExpnsCode"
            WHERE E."DocEntry" = ? AND IFNULL(E."LineTotal", 0) != 0
            ORDER BY E."LineNum"
            """,
            (doc_entry,),
        )
        return ", ".join(name for (name,) in rows if name)

    def _hsn_summary(self, doc_entry: int, lines: list) -> list:
        """The GST summary strip: one row per HSN code and rate on the order.

        Grouped on the rate as well as the code because one HSN can carry two
        rates on the same order, and a single row would have to pick one of them
        and misstate the tax on the other.

        This strip covers the goods only, so its tax can be less than the tax
        rows beside it on an order that carries freight: a charge has no HSN
        code to file it under. That is the same split SAP's GST return makes.
        """
        rows = self._hsn_taxes(doc_entry, lines)
        summary = []
        for (hsn_code, rate), (taxable, tax) in sorted(rows.items()):
            summary.append({
                "hsn_code": hsn_code,
                "taxable_value": self._out(taxable),
                "tax_rate": f"{rate:.2f}",
                "total_tax": self._out(tax),
            })
        return summary

    def _hsn_taxes(self, doc_entry: int, lines: list) -> dict:
        """Taxable value and tax per ``(HSN, rate)``, keyed off the order's lines.

        The rate on a line is the sum of its components' rates — an intra-state
        order's 9% CGST and 9% SGST make one 18.00 row, which is how the printed
        strip reads.
        """
        if not lines:
            return {}

        rows = self._query(
            """
            SELECT T."LineNum", SUM(IFNULL(T."TaxRate", 0)), SUM(IFNULL(T."TaxSum", 0))
            FROM "{schema}"."POR4" T
            WHERE T."DocEntry" = ? AND T."RelateType" = 1
            GROUP BY T."LineNum"
            """,
            (doc_entry,),
        )
        per_line = {
            int(line_num): (self._num(rate), self._num(tax))
            for line_num, rate, tax in rows
        }

        grouped: dict = {}
        for line in lines:
            rate, tax = per_line.get(line["sno"] - 1, (Decimal(0), Decimal(0)))
            key = (line["hsn_code"], rate)
            taxable, running_tax = grouped.get(key, (Decimal(0), Decimal(0)))
            grouped[key] = (taxable + line["_taxable"], running_tax + tax)
        return grouped

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
            logger.error(
                "SAP HANA connection failed while reading a purchase order print: %s", e
            )
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA purchase order print query failed: %s", e)
            raise SAPDataError("Failed to read the purchase order from SAP.") from e
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
