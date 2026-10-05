"""Raising a bill summary, getting it approved, and posting it to SAP.

The dispatch desk raises the sheet — usually a whole truck at a time, off the
back of linking a vehicle to its bills — and sends it to the warehouse. The
warehouse sets the dispatch date and approves. **That approval is what writes to
SAP**, and nothing before it does: the dispatch date is the one thing SAP is
really being told, it is write-once there, and it is not the dispatch desk's to
give. A sheet sitting with the warehouse has therefore touched SAP not at all,
which is also why sending it back costs nothing.

The app fills in everything the dispatch module already knows about the bill; the
user supplies the rest — in practice the bilty, which is raised once the truck is
loaded. The bilty is not demanded when the sheet is raised (the truck may not
have its LR yet) but it is demanded at approval, because that is the request SAP
will refuse without one.

Three things about SAP shape this code, all established by trying it against the
live Service Layer rather than reading documentation. The first two attempts were
refused, which is how the real contract surfaced:

**A dispatch date alone is rejected.** `SBO_SP_TRANSACTIONNOTIFICATION` answers
`(1300012) Please update the dispatch qty` unless every line of the invoice
carries a non-zero `INV1.U_Disp_Qty`.

**The bilty is effectively mandatory.** Without `U_BilltyNumber`, rule `1300016`
demands transporter, driver, vehicle, bilty date, both godown floor flags, mobile
number *and* `U_Recv_Date` — and setting `U_Recv_Date` then trips
`130001002 Please Attach its Receiving`, which wants a real file attachment. That
path is closed to automation. This is why the form insists on a bilty: it is not
a preference, it is the only way SAP will take the posting.

**The field names are misspelled, and not identically in every company.**
`U_Dipatch_Date` (not Dispatch) everywhere; but the bilty is `U_BilltyNumber` in
Oil and Mart and `U_BiltyNumber` in Beverages, the vehicle `U_VehicleNoM` against
`U_VechileNom` — and the Service Layer discards a property the company does not
have without saying so, which is why every Beverages invoice this module stamped
came out with no bilty on it at all. The names are therefore resolved against the
company's own OINV rather than assumed; see `hana_reader.DISPATCH_STAMP_COLUMNS`.

**The stamp is write-once.** The same procedure compares an updated invoice with
its own previous version and refuses (`1395111`-`1395117`) if the driver,
transporter, vehicle, bilty number, bilty date, dispatch date or mobile has
changed once it holds a value — refusing the WHOLE update, so a bilty date left
over from an earlier dispatch takes the dispatch date and every line quantity
down with it. What SAP already holds is therefore read first and left alone.

SAP also refuses a dispatch date earlier than the invoice date (`1300014`), so
that is checked here rather than being discovered at the end.
"""

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

import requests
import urllib3
from django.db import IntegrityError, transaction
from django.utils import timezone

from company.models import Company
from gate_core.services.box_packing import split_line, split_with_pieces_per_box
from sap_client.client import SAPClient
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .hana_reader import HanaDispatchBillReader
from .models import DispatchPlan
from .models_bill_summary import (
    SAP_SOURCE,
    BillSummary,
    BillSummaryLine,
    BillSummarySapStatus,
    BillSummaryStatus,
)
from .notifications import (
    notify_bill_summary_decided,
    notify_bill_summary_submitted,
)

logger = logging.getLogger(__name__)

urllib3.disable_warnings()


class BillSummaryError(Exception):
    """Something the user asked for that cannot be done, with the reason."""


def _dec(value, field: str) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, TypeError, ValueError):
        raise BillSummaryError(f"{field} is not a number: {value!r}")


class BillSummaryService:
    def __init__(self, company_code: str, user=None):
        self.company_code = company_code
        self.user = user
        self._reader = None

    @property
    def company(self) -> Company:
        return Company.objects.get(code=self.company_code)

    @property
    def reader(self) -> HanaDispatchBillReader:
        if self._reader is None:
            self._reader = HanaDispatchBillReader(CompanyContext(self.company_code))
        return self._reader

    # ------------------------------------------------------------------
    # search a bill and prefill
    # ------------------------------------------------------------------

    def lookup(self, bill_number: str) -> dict:
        """Everything needed to fill the form for one bill.

        Returns the invoice's lines plus a `prefill` block taken from the
        dispatch plan, and `missing` naming the fields the user still has to
        supply. Naming them up front is the point of the screen: the operator
        should see "the bilty is missing" rather than discovering it when SAP
        refuses the posting.
        """
        bill_number = (bill_number or "").strip()
        if not bill_number:
            raise BillSummaryError("Enter a bill number.")

        bill = self.reader.get_bill_by_number(bill_number)
        if not bill:
            raise BillSummaryError(f"No invoice {bill_number} in SAP for this company.")

        doc_entry = bill["doc_entry"]
        lines = self.reader.list_pickable_lines([doc_entry])
        if not lines:
            raise BillSummaryError(f"Invoice {bill_number} has no lines to fetch.")

        plan = (
            DispatchPlan.objects.filter(
                company=self.company, sap_invoice_doc_entry=doc_entry
            )
            .select_related(
                "vehicle", "transporter", "driver",
                "linked_vehicle_entry__vehicle", "linked_vehicle_entry__driver",
            )
            .first()
        )

        existing = self._live_summary(doc_entry)

        prefill = self._prefill(plan, lines)
        # The bilty is the usual gap, and it is the one the warehouse cannot
        # approve without. The dispatch date is not listed: it is nobody's to
        # supply on this screen, so calling it "missing" would be a gap the user
        # cannot close.
        missing = [name for name in ("bilty_no",) if not prefill.get(name)]

        return {
            "doc_entry": doc_entry,
            "doc_num": str(bill.get("doc_num") or bill_number),
            "doc_date": bill.get("doc_date"),
            "customer_code": bill.get("card_code") or lines[0]["card_code"],
            "customer_name": bill.get("card_name") or lines[0]["card_name"],
            "warehouse_codes": sorted(
                {line["warehouse_code"] for line in lines if line["warehouse_code"]}
            ),
            "has_plan": plan is not None,
            "prefill": prefill,
            "missing": missing,
            "existing_summary": existing.entry_no if existing else "",
            "existing_summary_id": existing.id if existing else None,
            "lines": [
                {
                    "sap_line_num": line["line_num"],
                    "item_code": line["item_code"],
                    "item_name": line["item_name"],
                    "uom": line["uom"],
                    "warehouse_code": line["warehouse_code"],
                    "invoice_qty": line["quantity"],
                    "pcs_per_box": line["pcs_per_box"],
                    "boxes": line["boxes"],
                    "litres": line["litres"],
                }
                for line in lines
            ],
        }

    def _prefill(self, plan, lines) -> dict:
        """What the dispatch module already knows, from every place it keeps it.

        Three sources, in order of authority:

        1. The **dispatch plan** — planning is where the dispatch is decided.
        2. The plan's **linked gate entry** — and this one is load-bearing for the
           driver. Planning books a vehicle and a transporter but hardly ever a
           driver (on live data: vehicle on 87% of plans, driver on 1%), because
           the driver is only known when the truck actually turns up and the gate
           records it. Reading only `plan.driver` therefore left the driver blank
           on almost every sheet.
        3. **SAP's own UDFs**, for a bill somebody already filled in by hand.

        The dispatch date is NOT among them, from any source, and no longer
        could be: it is the warehouse's to give at approval. That was already
        the right answer before the approval step existed — it is written into
        SAP where it can never be changed again, a plan's date is a plan, often
        days old and routinely wrong by the time the truck is loaded, and an
        offered date gets accepted without being read — and it is now also a
        field this desk simply does not fill.
        """
        sap_bilty = lines[0].get("sap_bilty_no", "") if lines else ""

        if plan is None:
            return {
                "dispatch_date": None,
                "bilty_no": sap_bilty,
                "bilty_date": None,
                "transporter_name": "",
                "vehicle_no": "",
                "driver_name": "",
                "driver_mobile": "",
            }

        entry = getattr(plan, "linked_vehicle_entry", None)
        transporter = getattr(plan, "transporter", None)
        # Fall through to the gate entry for anything planning left blank.
        vehicle = getattr(plan, "vehicle", None) or getattr(entry, "vehicle", None)
        driver = getattr(plan, "driver", None) or getattr(entry, "driver", None)

        return {
            "dispatch_date": None,
            "bilty_no": (plan.bilty_no or "").strip() or sap_bilty,
            "bilty_date": plan.bilty_date,
            "transporter_name": getattr(transporter, "name", "") or "",
            "vehicle_no": getattr(vehicle, "vehicle_number", "") or "",
            "driver_name": getattr(driver, "name", "") or "",
            "driver_mobile": getattr(driver, "mobile_no", "") or "",
        }

    # ------------------------------------------------------------------
    # bills stamped straight into SAP
    # ------------------------------------------------------------------
    #
    # Not every dispatch goes through this module. The flow it replaced is still
    # in use - the details typed onto the invoice in SAP and SAP's own saved
    # query printed - and those bills are just as much "a sheet the floor worked
    # from" as the app's own. They are read out of SAP on demand and presented in
    # the same shape as an app sheet, so neither the list nor the sheet itself
    # has to know which kind it is holding.
    #
    # They are a projection, not a record: nothing is written until somebody acts
    # on one, at which point `adopt_sap_summary` puts it on the app's books and
    # every action from there is the ordinary one.

    def list_sap_summaries(self, filters: dict) -> list:
        """Dispatches stamped in SAP that the app has no live sheet for.

        A bill the app already has a sheet for is dropped rather than listed
        twice: it is the same dispatch, and showing both would have the day's
        work counted twice on a screen whose whole job is to say what went out.
        """
        rows = self.reader.list_stamped_bills(filters)
        if not rows:
            return []
        taken = set(
            BillSummary.objects.filter(
                company=self.company,
                is_active=True,
                sap_invoice_doc_entry__in=[row["doc_entry"] for row in rows],
            )
            .exclude(status=BillSummaryStatus.CANCELLED)
            .values_list("sap_invoice_doc_entry", flat=True)
        )
        return [self._sap_row(row) for row in rows if row["doc_entry"] not in taken]

    def get_sap_summary(self, doc_entry: int) -> dict:
        """One stamped bill, with its lines, shaped like an app sheet."""
        rows = self.reader.list_stamped_bills({"doc_entry": int(doc_entry)})
        if not rows:
            raise BillSummaryError(
                f"Invoice {doc_entry} carries no dispatch in SAP for this company."
            )
        header = rows[0]
        lines = self._sap_lines(int(doc_entry))
        row = self._sap_row(header, lines=lines)
        # Only the opened sheet pays for these: the printed layout needs them,
        # a list of two hundred rows does not, and each is its own SAP query.
        row["delivery_address"] = header.get("ship_to_address") or ""
        row["branch_gstin"] = self._branch_gstin(header.get("branch_id"))
        row["company_legal_name"] = self._company_legal_name()
        row["lines"] = lines
        existing = (
            BillSummary.objects.filter(
                company=self.company,
                sap_invoice_doc_entry=int(doc_entry),
                is_active=True,
            )
            .exclude(status=BillSummaryStatus.CANCELLED)
            .first()
        )
        # Opened by URL after somebody took it over elsewhere: name the sheet it
        # became rather than showing a second copy of the same dispatch.
        row["app_summary_id"] = existing.id if existing else None
        return row

    def _sap_lines(self, doc_entry: int) -> list:
        """The invoice's lines, split into boxes exactly as an app sheet is."""
        out = []
        for line in self.reader.list_pickable_lines([doc_entry]):
            # SAP demands a dispatch quantity before it accepts a dispatch date,
            # so a stamped bill normally carries one. Where it does not - stamped
            # before that rule, or by a route that dodged it - the billed
            # quantity is the honest reading, and it is what the app defaults to
            # on its own sheets.
            dispatch_qty = line["dispatched_qty"] or line["quantity"]
            packing = split_line(
                dispatch_qty,
                line.get("sal_factor2"),
                line["item_name"],
                line.get("sal_factor3"),
            )
            out.append(
                {
                    # The SAP line number: these rows have no BillSummaryLine to
                    # have an id of, and the screen only needs something stable
                    # to key the list on.
                    "id": line["line_num"],
                    "sap_line_num": line["line_num"],
                    "item_code": line["item_code"],
                    "item_name": line["item_name"],
                    "uom": line["uom"],
                    "warehouse_code": line["warehouse_code"],
                    "invoice_qty": line["quantity"],
                    "pcs_per_box": packing.pieces_per_box or 0,
                    "boxes": packing.boxes,
                    "loose_qty": packing.loose,
                    "litres": line["litres"],
                    "gross_weight": line.get("gross_weight") or 0,
                    "dispatch_qty": dispatch_qty,
                    "is_short": dispatch_qty < line["quantity"],
                }
            )
        return out

    def _sap_row(self, header: dict, lines=None) -> dict:
        """A stamped bill in the shape `BillSummaryListSerializer` returns.

        `id` is null and `key` carries the `sap-<DocEntry>` the screen routes on:
        there is no record to have a primary key yet, and handing out the
        DocEntry as one would open somebody else's sheet.
        """
        if lines is None:
            totals = {
                "lines": header["line_count"],
                "boxes": header["total_boxes"],
                "litres": header["total_litres"],
                "invoice_qty": Decimal("0"),
                "dispatch_qty": Decimal("0"),
                "loose_qty": Decimal("0"),
                "gross_weight": Decimal("0"),
            }
        else:
            totals = {
                "lines": len(lines),
                "boxes": sum(line["boxes"] for line in lines),
                "litres": sum(line["litres"] for line in lines),
                "invoice_qty": sum(line["invoice_qty"] for line in lines),
                "dispatch_qty": sum(line["dispatch_qty"] for line in lines),
                "loose_qty": sum(line["loose_qty"] for line in lines),
                "gross_weight": sum(line["gross_weight"] for line in lines),
            }
        company = self.company
        return {
            "id": None,
            "key": f"sap-{header['doc_entry']}",
            "source": SAP_SOURCE,
            # Numbered by the bill, because that is the only number this dispatch
            # has ever had. Inventing a BS- number would make it look like a sheet
            # the app issued.
            "entry_no": f"SAP-{header['doc_num']}",
            "company": company.id,
            "company_code": company.code,
            "sap_invoice_doc_entry": header["doc_entry"],
            "sap_invoice_doc_num": header["doc_num"],
            "customer_code": header["card_code"],
            "customer_name": header["card_name"],
            "delivery_address": "",
            "invoice_date": header.get("doc_date"),
            "bill_amount": header.get("doc_total") or 0,
            "branch_name": header.get("branch_name") or "",
            "branch_gstin": "",
            "company_legal_name": "",
            "warehouse_codes": header.get("warehouses") or "",
            "dispatch_date": header.get("dispatch_date"),
            "bilty_no": header.get("bilty_no") or "",
            "bilty_date": header.get("bilty_date"),
            "transporter_name": header.get("transporter_name") or "",
            "vehicle_no": header.get("vehicle_no") or "",
            "driver_name": header.get("driver_name") or "",
            "driver_mobile": header.get("driver_mobile") or "",
            # The dispatch is live and SAP is where it lives - which is what an
            # approved app sheet that posted cleanly reports, so it reads the
            # same. It never went through the warehouse's approval here because
            # it never went through this app at all; the dispatch date it is
            # showing is the one somebody typed straight into SAP.
            "status": BillSummaryStatus.APPROVED,
            "sap_status": BillSummarySapStatus.POSTED,
            "sap_error": "",
            "sap_note": "",
            "sap_posted_at": None,
            "issued_by_name": "",
            "picked_by_name": "",
            "approved_by_name": "",
            "rejected_by_name": "",
            "printed_by_name": "",
            "issued_at": None,
            "submitted_at": None,
            "approved_at": None,
            "rejected_at": None,
            "printed_at": None,
            "picked_at": None,
            # Nothing here is this app's to change: the dispatch lives in SAP
            # and its stamp is write-once there.
            "is_editable": False,
            "remarks": "",
            "cancel_reason": "",
            "reject_reason": "",
            "totals": totals,
            "app_summary_id": None,
        }

    @transaction.atomic
    def adopt_sap_summary(self, doc_entry: int) -> BillSummary:
        """Put a bill stamped by hand in SAP onto the app's books.

        Acting on one of these - cancelling it, in practice - has to act on a
        record, so the projection is made real first. SAP is not written to here:
        it already holds the stamp, and those fields are write-once, so the sheet
        is recorded as posted rather than posted again.

        `issued_by` stays empty on purpose. Whoever presses the button did not
        issue this sheet; somebody typed it into SAP, and the record should not
        claim otherwise.
        """
        doc_entry = int(doc_entry)
        existing = (
            BillSummary.objects.filter(
                company=self.company, sap_invoice_doc_entry=doc_entry, is_active=True
            )
            .exclude(status=BillSummaryStatus.CANCELLED)
            .first()
        )
        # Two people opening the same stamped bill is not a conflict: the first
        # takes it over, the second lands on the sheet that already exists.
        if existing:
            return existing

        rows = self.reader.list_stamped_bills({"doc_entry": doc_entry})
        if not rows:
            raise BillSummaryError(
                f"Invoice {doc_entry} carries no dispatch in SAP for this company."
            )
        header = rows[0]
        dispatch_date = self._as_date(header.get("dispatch_date"))
        if not dispatch_date:
            raise BillSummaryError("That bill has no dispatch date in SAP.")

        lines = self._sap_lines(doc_entry)
        if not lines:
            raise BillSummaryError("That bill has no lines to fetch.")

        summary = self._create_numbered(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=header["doc_num"],
            customer_code=header["card_code"],
            customer_name=header["card_name"],
            delivery_address=header.get("ship_to_address") or "",
            invoice_date=self._as_date(header.get("doc_date")),
            bill_amount=header.get("doc_total") or 0,
            branch_name=header.get("branch_name") or "",
            branch_gstin=self._branch_gstin(header.get("branch_id")),
            company_legal_name=self._company_legal_name(),
            warehouse_codes=header.get("warehouses") or "",
            dispatch_date=dispatch_date,
            bilty_no=header.get("bilty_no") or "",
            bilty_date=self._as_date(header.get("bilty_date")),
            transporter_name=header.get("transporter_name") or "",
            vehicle_no=header.get("vehicle_no") or "",
            driver_name=header.get("driver_name") or "",
            driver_mobile=header.get("driver_mobile") or "",
            remarks=(
                "Dispatch was typed straight into SAP; taken onto the app's "
                f"books by {getattr(self.user, 'full_name', '') or 'a user'}."
            ),
            issued_by=None,
            # Approved by definition: SAP already holds the dispatch date, which
            # is the only thing approval decides. `approved_by` stays empty for
            # the same reason `issued_by` does — nobody here approved it.
            status=BillSummaryStatus.APPROVED,
            approved_at=timezone.now(),
            sap_status=BillSummarySapStatus.POSTED,
        )
        BillSummaryLine.objects.bulk_create(
            [
                BillSummaryLine(
                    summary=summary,
                    sap_line_num=line["sap_line_num"],
                    item_code=line["item_code"],
                    item_name=line["item_name"],
                    uom=line["uom"],
                    warehouse_code=line["warehouse_code"],
                    invoice_qty=line["invoice_qty"],
                    pcs_per_box=line["pcs_per_box"],
                    boxes=line["boxes"],
                    loose_qty=line["loose_qty"],
                    litres=line["litres"],
                    gross_weight=line["gross_weight"],
                    dispatch_qty=line["dispatch_qty"],
                )
                for line in lines
            ]
        )
        logger.info(
            "Bill summary %s adopted from the SAP stamp on bill %s",
            summary.entry_no,
            summary.sap_invoice_doc_num,
        )
        return summary

    # ------------------------------------------------------------------
    # generate
    # ------------------------------------------------------------------

    @transaction.atomic
    def generate(self, data: dict) -> BillSummary:
        """Raise the sheet for one bill and send it to the warehouse.

        Nothing is written to SAP here. The sheet carries no dispatch date yet
        either — that is what it is being sent across to collect.

        The bilty is not demanded at this point even though SAP will not take the
        posting without one. The truck's LR is often not raised when the load is
        being put together, and refusing the sheet over it would only push the
        dispatch desk back to typing dispatches into SAP by hand. It is demanded
        at approval instead, where it is actually needed.
        """
        doc_entry = data.get("sap_invoice_doc_entry")
        if not doc_entry:
            raise BillSummaryError("Which bill is this summary for?")

        clash = self._live_summary(doc_entry)
        if clash:
            raise BillSummaryError(
                f"{clash.entry_no} already covers this bill. Cancel it first to reissue."
            )

        summary = self._raise_sheet(doc_entry, data)
        transaction.on_commit(lambda: notify_bill_summary_submitted([summary]))
        logger.info(
            "Bill summary %s raised for bill %s and sent to the warehouse",
            summary.entry_no, summary.sap_invoice_doc_num,
        )
        return summary

    def _live_summary(self, doc_entry: int):
        """The sheet already covering this bill, cancelled ones aside."""
        return (
            BillSummary.objects.filter(
                company=self.company,
                sap_invoice_doc_entry=int(doc_entry),
                is_active=True,
            )
            .exclude(status=BillSummaryStatus.CANCELLED)
            .first()
        )

    def _raise_sheet(self, doc_entry: int, data: dict) -> BillSummary:
        """The sheet and its lines, snapshotted from SAP. No SAP write.

        Shared by the one-bill form and the whole-truck submission, so a sheet
        raised off a vehicle is the same record as one typed by hand rather than
        a thinner cousin of it.
        """
        doc_entry = int(doc_entry)
        bill = self.reader.get_bill_by_number(
            str(data.get("sap_invoice_doc_num") or "")
        ) if data.get("sap_invoice_doc_num") else None
        lines = self.reader.list_pickable_lines([doc_entry])
        if not lines:
            raise BillSummaryError("That bill has no lines to fetch.")

        header = bill or {}
        now = timezone.now()
        summary = self._create_numbered(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(
                data.get("sap_invoice_doc_num") or lines[0]["doc_num"]
            ),
            customer_code=lines[0]["card_code"],
            customer_name=lines[0]["card_name"],
            # Snapshotted so the printed sheet reproduces SAP's Bill Summary
            # layout without going back to SAP every time it is reprinted.
            delivery_address=header.get("ship_to_address") or "",
            invoice_date=self._as_date(header.get("doc_date")),
            bill_amount=header.get("doc_total") or 0,
            branch_name=header.get("branch_name") or "",
            branch_gstin=self._branch_gstin(header.get("branch_id")),
            company_legal_name=self._company_legal_name(),
            warehouse_codes=", ".join(
                sorted({line["warehouse_code"] for line in lines if line["warehouse_code"]})
            ),
            # The warehouse's to give.
            dispatch_date=None,
            bilty_no=(data.get("bilty_no") or "").strip(),
            bilty_date=data.get("bilty_date"),
            transporter_name=(data.get("transporter_name") or "").strip(),
            vehicle_no=(data.get("vehicle_no") or "").strip(),
            driver_name=(data.get("driver_name") or "").strip(),
            driver_mobile=(data.get("driver_mobile") or "").strip(),
            remarks=data.get("remarks") or "",
            status=BillSummaryStatus.PENDING_APPROVAL,
            issued_by=self.user,
            issued_at=now,
            submitted_at=now,
        )
        self._write_lines(summary, lines, data.get("lines") or [])
        return summary

    def _create_numbered(self, **fields) -> BillSummary:
        """Create the sheet, retrying if somebody else took the number first.

        `entry_no` is read-max-then-add-one, which two desks raising sheets in
        the same second can both win. That was survivable while sheets were typed
        one at a time; a whole truck submitted at once makes it ordinary.
        """
        for attempt in range(5):
            try:
                with transaction.atomic():
                    return BillSummary.objects.create(
                        entry_no=BillSummary.generate_entry_no(), **fields
                    )
            except IntegrityError:
                if attempt == 4:
                    raise
                logger.info("Bill summary entry number taken; retrying")
        raise BillSummaryError("Could not allocate a bill summary number.")

    def _write_lines(self, summary: BillSummary, lines: list, overrides_in: list) -> None:
        """Snapshot the invoice's lines onto the sheet, short quantities and all."""
        overrides = {
            int(row["sap_line_num"]): _dec(row.get("dispatch_qty"), "Dispatch quantity")
            for row in overrides_in
            if row.get("sap_line_num") is not None
        }
        objects = []
        for line in lines:
            dispatch_qty = overrides.get(line["line_num"], line["quantity"])
            # SAP's own split: full boxes plus leftover pieces, with SalFactor2=1
            # meaning "not boxed at all" — except where the billed unit IS a box,
            # which SAP marks SalFactor3 > 1 and which CSD stock all carries.
            # Never quantity/per-box, which would print a fraction of a carton.
            packing = split_line(
                dispatch_qty,
                line.get("sal_factor2"),
                line["item_name"],
                line.get("sal_factor3"),
            )
            if dispatch_qty < 0:
                raise BillSummaryError(
                    f"Dispatch quantity for {line['item_code']} cannot be negative."
                )
            if dispatch_qty > line["quantity"]:
                raise BillSummaryError(
                    f"Cannot dispatch {dispatch_qty} of {line['item_code']}: the bill "
                    f"is only for {line['quantity']}."
                )
            objects.append(
                BillSummaryLine(
                    summary=summary,
                    sap_line_num=line["line_num"],
                    item_code=line["item_code"],
                    item_name=line["item_name"],
                    uom=line["uom"],
                    warehouse_code=line["warehouse_code"],
                    invoice_qty=line["quantity"],
                    pcs_per_box=packing.pieces_per_box or 0,
                    boxes=packing.boxes,
                    loose_qty=packing.loose,
                    litres=line["litres"],
                    gross_weight=line.get("gross_weight") or 0,
                    dispatch_qty=dispatch_qty,
                )
            )
        if not any(obj.dispatch_qty > 0 for obj in objects):
            raise BillSummaryError(
                "Every line is zero, so SAP would refuse this. Set at least one "
                "dispatch quantity."
            )
        BillSummaryLine.objects.bulk_create(objects)

    # ------------------------------------------------------------------
    # a whole truck at once
    # ------------------------------------------------------------------

    def submit_bills(self, doc_entries: list, dry_run: bool = False) -> dict:
        """Raise a sheet for each of a truck's bills and send the lot across.

        Called the moment a vehicle is linked to its bills, which is when the
        dispatch desk knows what the load is. Nobody wants to type eight sheets,
        and eight sheets typed separately is eight chances to key a different
        vehicle number onto the same truck.

        `dry_run` answers the popup's question — how many bills would this raise
        a sheet for — off the app's own tables alone. Reaching into SAP for a
        count the user has not yet agreed to is a HANA query per bill for a
        dialog they may well dismiss.

        One bill failing does not take the rest down: each is its own record and
        its own SAP invoice, and a truck half of whose sheets exist is far easier
        to finish than one whose submission was refused outright. What was
        skipped is named, per bill, rather than counted.
        """
        seen, wanted = set(), []
        for raw in doc_entries or []:
            doc_entry = int(raw)
            if doc_entry not in seen:
                seen.add(doc_entry)
                wanted.append(doc_entry)
        if not wanted:
            raise BillSummaryError("No bills to raise a summary for.")

        plans = {
            plan.sap_invoice_doc_entry: plan
            for plan in DispatchPlan.objects.filter(
                company=self.company, sap_invoice_doc_entry__in=wanted
            ).select_related(
                "vehicle", "transporter", "driver",
                "linked_vehicle_entry__vehicle", "linked_vehicle_entry__driver",
            )
        }
        taken = {
            summary.sap_invoice_doc_entry: summary
            for summary in BillSummary.objects.filter(
                company=self.company,
                is_active=True,
                sap_invoice_doc_entry__in=wanted,
            ).exclude(status=BillSummaryStatus.CANCELLED)
        }

        eligible, skipped, created = [], [], []
        for doc_entry in wanted:
            plan = plans.get(doc_entry)
            label = getattr(plan, "sap_invoice_doc_num", "") or str(doc_entry)
            existing = taken.get(doc_entry)
            if existing:
                skipped.append({
                    "doc_entry": doc_entry,
                    "doc_num": existing.sap_invoice_doc_num or label,
                    "reason": f"{existing.entry_no} already covers this bill.",
                })
                continue
            if plan is None:
                skipped.append({
                    "doc_entry": doc_entry,
                    "doc_num": label,
                    "reason": "No dispatch plan for this bill in this company.",
                })
                continue
            eligible.append({
                "doc_entry": doc_entry,
                "doc_num": plan.sap_invoice_doc_num or label,
                "customer_name": plan.customer_name or "",
                "vehicle_no": plan.vehicle_no,
                "bilty_no": plan.bilty_no or "",
            })
            if dry_run:
                continue
            try:
                with transaction.atomic():
                    summary = self._raise_sheet(doc_entry, self._plan_data(plan))
            except (BillSummaryError, SAPConnectionError, SAPDataError) as exc:
                skipped.append({
                    "doc_entry": doc_entry,
                    "doc_num": plan.sap_invoice_doc_num or label,
                    "reason": str(exc),
                })
                continue
            created.append(summary)

        if created:
            summaries = list(created)
            transaction.on_commit(lambda: notify_bill_summary_submitted(summaries))
            logger.info(
                "%s bill summaries raised from a vehicle link and sent to the warehouse",
                len(created),
            )
        return {
            "dry_run": bool(dry_run),
            "eligible": eligible,
            "skipped": skipped,
            "created": created,
        }

    @staticmethod
    def _plan_data(plan) -> dict:
        """The sheet's transport details, as the dispatch plan holds them.

        The driver is read through the gate entry as well as the plan: planning
        books a vehicle and a transporter but hardly ever a driver, because the
        driver is only known when the truck turns up and the gate records it.
        """
        entry = getattr(plan, "linked_vehicle_entry", None)
        vehicle = plan.vehicle or getattr(entry, "vehicle", None)
        driver = plan.driver or getattr(entry, "driver", None)
        return {
            "sap_invoice_doc_num": plan.sap_invoice_doc_num or "",
            "bilty_no": (plan.bilty_no or "").strip(),
            "bilty_date": plan.bilty_date,
            "transporter_name": getattr(plan.transporter, "name", "") or "",
            "vehicle_no": getattr(vehicle, "vehicle_number", "") or "",
            "driver_name": getattr(driver, "name", "") or "",
            "driver_mobile": getattr(driver, "mobile_no", "") or "",
            "remarks": "",
        }

    def _company_legal_name(self) -> str:
        """Best effort — a missing name must not stop the sheet being produced."""
        try:
            return self.reader.company_legal_name()
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the company name for %s", self.company_code)
            return ""

    def _branch_gstin(self, branch_id) -> str:
        """Best effort — a missing GST must not stop the sheet being produced."""
        try:
            return self.reader.branch_gstin(branch_id)
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the branch GST for %s", branch_id)
            return ""

    @staticmethod
    def _as_date(value):
        if not value:
            return None
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
        return getattr(value, "date", lambda: value)()

    @staticmethod
    def _check_dispatch_date(dispatch_date, doc_date) -> None:
        """SAP refuses a dispatch date before the invoice date (rule 1300014)."""
        if not doc_date:
            return
        if isinstance(doc_date, str):
            doc_date = date.fromisoformat(doc_date[:10])
        if hasattr(doc_date, "date"):
            doc_date = doc_date.date()
        if dispatch_date < doc_date:
            raise BillSummaryError(
                f"Dispatch date {dispatch_date} is before the bill's own date "
                f"{doc_date}; SAP will not accept that."
            )

    # ------------------------------------------------------------------
    # post to SAP
    # ------------------------------------------------------------------

    def post_to_sap(self, summary_id: int) -> BillSummary:
        """Make SAP agree with the sheet.

        A live sheet stamps the invoice; a cancelled one clears it again. Both
        directions go through here so the retry action means "reconcile with
        SAP" whatever state the sheet is in — a cancelled sheet whose clearing
        failed needs chasing just as much as a live one that never posted.
        """
        summary = BillSummary.objects.filter(pk=summary_id).first()
        if summary is None:
            raise BillSummaryError("Bill summary not found.")
        clearing = summary.status == BillSummaryStatus.CANCELLED
        if not clearing and summary.dispatch_date is None:
            # A sheet still with the warehouse. There is nothing to stamp: the
            # dispatch date is the whole of the message, and it has not been
            # given yet.
            raise BillSummaryError(
                f"{summary.entry_no} has not been approved, so it has no dispatch "
                "date to put on the bill."
            )

        kept, dropped = [], []
        try:
            kept, dropped = self._patch_invoice(summary, clear=clearing)
        except (SAPConnectionError, SAPDataError, BillSummaryError) as exc:
            summary.sap_status = BillSummarySapStatus.FAILED
            summary.sap_error = str(exc)[:4000]
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            logger.exception("Unexpected SAP failure posting %s", summary.entry_no)
            summary.sap_status = BillSummarySapStatus.FAILED
            summary.sap_error = str(exc)[:4000]
        else:
            # Cleared is NOT_POSTED, not POSTED: the invoice no longer carries a
            # dispatch, and saying otherwise would hide it from the "not in SAP"
            # view that exists to catch exactly this.
            summary.sap_status = (
                BillSummarySapStatus.NOT_POSTED if clearing
                else BillSummarySapStatus.POSTED
            )
            summary.sap_error = ""
            summary.sap_posted_at = None if clearing else timezone.now()
        # Said on the sheet rather than only in the log: either way the driver is
        # carrying a document that disagrees with the invoice, and the difference
        # should be visible to whoever holds both.
        notes = []
        if kept:
            # SAP kept its own values for these, and they cannot be changed.
            notes.append(
                "SAP keeps its existing " + ", ".join(kept) + " on this bill; "
                "once set, these cannot be changed."
            )
        if dropped:
            # Longer than the UDF holds. Left off so the rest of the stamp - the
            # dispatch date and the line quantities - could still be written.
            notes.append(
                "Too long for SAP's own field, so not stamped on the bill: "
                + ", ".join(dropped) + "."
            )
        summary.sap_note = " ".join(notes)
        summary.save(
            update_fields=[
                "sap_status", "sap_error", "sap_posted_at", "sap_note", "updated_at",
            ]
        )
        return summary

    def _patch_invoice(self, summary: BillSummary, *, clear: bool = False) -> tuple:
        """The write itself. See the module docstring for why it looks like this.

        Returns whatever SAP is keeping in place of the sheet's own values, and
        whatever would not fit in its fields, so the sheet can say so instead of
        implying the invoice matches it.

        `clear` takes the stamp back off, which a cancelled sheet needs: leaving
        a dispatch date on an invoice nobody is dispatching is worse than never
        having written it. The date and the line quantities must go together
        here too — the notification rule fires on a date with no quantity, so
        both are cleared in the same request. Tested against SAP; it accepts it.
        """
        sl = CompanyContext(self.company_code).service_layer

        session = requests.Session()
        session.verify = False
        login = session.post(
            f"{sl['base_url']}/b1s/v2/Login",
            json={
                "CompanyDB": sl["company_db"],
                "UserName": sl["username"],
                "Password": sl["password"],
            },
            timeout=30,
        )
        if login.status_code != 200:
            raise SAPConnectionError(f"SAP login failed ({login.status_code}).")

        if clear:
            payload = {
                "U_Dipatch_Date": None,
                "DocumentLines": [
                    {"LineNum": line.sap_line_num, "U_Disp_Qty": 0}
                    for line in summary.active_lines
                ],
            }
            # The bilty is deliberately left alone: it is the transporter's
            # number for a real consignment note, not ours to erase.
            response = session.patch(
                f"{sl['base_url']}/b1s/v2/Invoices({summary.sap_invoice_doc_entry})",
                json=payload,
                timeout=180,
            )
            if response.status_code not in (200, 204):
                raise SAPDataError(self._sap_message(response))
            return [], []

        payload, kept, dropped = self._stamp_payload(
            summary,
            self.reader.dispatch_stamp_columns(),
            self.reader.invoice_dispatch_stamp(summary.sap_invoice_doc_entry),
            self.reader.dispatch_stamp_sizes(),
        )

        response = session.patch(
            f"{sl['base_url']}/b1s/v2/Invoices({summary.sap_invoice_doc_entry})",
            json=payload,
            timeout=180,
        )
        if response.status_code not in (200, 204):
            raise SAPDataError(self._sap_message(response))
        return kept, dropped

    # The dispatch identity is write-once in SAP. `SBO_SP_TRANSACTIONNOTIFICATION`
    # compares an updated A/R invoice against its own previous version and refuses
    # (1395111-1395117) if the driver, transporter, vehicle, bilty number, bilty
    # date, dispatch date or mobile has changed once it holds a value. It refuses
    # the WHOLE update, so a bilty date left over from an earlier dispatch takes
    # the dispatch date and every line quantity down with it — which is exactly
    # how a sheet ends up "not posted" over a field nobody was trying to change.
    _STAMP_LABELS = {
        "bilty_no": "bilty number",
        "bilty_date": "bilty date",
        "transporter_name": "transporter",
        "vehicle_no": "vehicle",
        "driver_name": "driver",
        "driver_mobile": "driver mobile",
    }

    def _stamp_payload(
        self, summary: BillSummary, columns: dict, existing: dict, sizes: dict | None = None
    ):
        """The PATCH body, what SAP is keeping, and what would not fit in it.

        Anything SAP already holds is left alone rather than overwritten: it
        cannot be changed, and trying is what fails the posting. Where its value
        differs from the sheet's, that is reported back so the difference between
        the printed sheet and the invoice is visible rather than silent.

        `sizes` is how wide each UDF actually is in this company's SAP. They are
        narrower than this app's own fields and differ between the companies, and
        SAP does not trim: one over-long value and the Service Layer refuses the
        whole request, so the dispatch date and every line quantity are lost over
        a driver's phone number. Anything too long is therefore left out and
        reported, never truncated - these fields are write-once, and half a phone
        number recorded forever is worse than none.
        """
        dispatch_column = columns.get("dispatch_date")
        if not dispatch_column:
            raise BillSummaryError(
                "This company's A/R invoice has no dispatch-date field to stamp."
            )

        # The one field that cannot simply be skipped: a sheet posted against
        # somebody else's dispatch date would be a lie, not a compromise.
        sap_date = existing.get("dispatch_date")
        if sap_date and sap_date != summary.dispatch_date:
            raise BillSummaryError(
                f"SAP already has {sap_date} as the dispatch date on this bill and "
                f"will not let it change to {summary.dispatch_date}. Reissue the "
                f"sheet for {sap_date}, or have the date corrected in SAP first."
            )

        payload = {
            # Misspelled in SAP. Copied exactly, on purpose.
            dispatch_column: summary.dispatch_date.strftime("%Y-%m-%d"),
            "DocumentLines": [
                {"LineNum": line.sap_line_num, "U_Disp_Qty": float(line.dispatch_qty)}
                for line in summary.active_lines
            ],
        }

        sizes = sizes or {}
        kept, dropped = [], []
        for field, value in (
            ("bilty_no", summary.bilty_no),
            ("bilty_date", summary.bilty_date),
            ("transporter_name", summary.transporter_name),
            ("vehicle_no", summary.vehicle_no),
            ("driver_name", summary.driver_name),
            ("driver_mobile", summary.driver_mobile),
        ):
            column = columns.get(field)
            held = existing.get(field)
            if held:
                if value and held != value:
                    kept.append(f"{self._STAMP_LABELS[field]} {held}")
                continue
            if not value:
                continue
            if not column:
                # A field this company simply does not have. Worth a line in the
                # log rather than a property SAP will discard without saying so.
                logger.warning(
                    "%s has no %s field; %s not stamped on invoice %s",
                    self.company_code, field, value, summary.sap_invoice_doc_num,
                )
                continue

            text = value.strftime("%Y-%m-%d") if field == "bilty_date" else value
            limit = sizes.get(field)
            if limit and len(text) > limit:
                if field == "bilty_no":
                    # The one that cannot just be left out: with no bilty number
                    # SAP demands a receiving attachment we have no way to supply,
                    # so the whole posting would fail anyway - and less clearly.
                    raise BillSummaryError(
                        f"SAP keeps only {limit} characters of a bilty number and "
                        f"this sheet's is {len(text)} ({text}). Correct the bilty "
                        "number on the sheet, then post again."
                    )
                logger.warning(
                    "%s: %s %r is longer than SAP's %s (%s); not stamped on invoice %s",
                    self.company_code, field, text, column, limit,
                    summary.sap_invoice_doc_num,
                )
                dropped.append(f"{self._STAMP_LABELS[field]} {text}")
                continue
            payload[column] = text

        return payload, kept, dropped

    @staticmethod
    def _sap_message(response) -> str:
        try:
            error = response.json().get("error", {})
            message = error.get("message")
            if isinstance(message, dict):
                message = message.get("value")
            return str(message or response.text)[:500]
        except Exception:  # noqa: BLE001
            return f"HTTP {response.status_code}: {response.text[:300]}"

    # ------------------------------------------------------------------
    # the bill itself
    # ------------------------------------------------------------------

    def invoice_print_payload(self, doc_entry: int) -> dict:
        """SAP's own TAX INVOICE for the bill a sheet was raised against.

        The summary is the picking sheet; this is the bill the customer gets, and
        until now the only way to print it was to open the invoice in SAP. It is
        the same sheet the A/R Invoice screen prints - the Crystal layout's own
        data source, read straight from HANA - asked for by the invoice's
        `DocEntry` so that it serves an app sheet and a dispatch stamped straight
        into SAP alike; the latter has no record here to key off.

        Read fresh every time rather than stored: an invoice can be amended or
        cancelled in SAP after the sheet was issued, and what gets handed to the
        driver has to be what SAP currently says.
        """
        doc_entry = int(doc_entry)
        state = self.reader.invoice_state(doc_entry)
        if not state:
            raise BillSummaryError(
                f"Invoice {doc_entry} is not in SAP for this company."
            )
        label = state["doc_num"] or doc_entry
        if state["is_cancelled"]:
            raise BillSummaryError(
                f"Invoice {label} was cancelled in SAP, so there is no bill to print."
            )

        payload = SAPClient(company_code=self.company_code).ar_invoice_print(doc_entry)
        if not payload:
            raise BillSummaryError(f"SAP has no invoice {label} for this company.")
        return payload

    # ------------------------------------------------------------------
    # the warehouse's decision
    # ------------------------------------------------------------------

    def approve(self, summary_ids: list, dispatch_date) -> list:
        """Set the dispatch date on each sheet and stamp SAP.

        One date across the batch, because that is how the decision is actually
        made: a truck goes out on a day, not each of its bills separately. The
        sheets are approved one at a time all the same, so one bill SAP refuses
        does not un-approve the seven beside it — the refusal is recorded on that
        sheet for retry, exactly as a failed posting always was.
        """
        if not dispatch_date:
            raise BillSummaryError("A dispatch date is required to approve.")
        ids = [int(value) for value in (summary_ids or [])]
        if not ids:
            raise BillSummaryError("No bill summaries to approve.")

        approved, refused = [], []
        for summary_id in ids:
            try:
                approved.append(self._approve_one(summary_id, dispatch_date))
            except BillSummaryError as exc:
                summary = BillSummary.objects.filter(
                    pk=summary_id, company=self.company
                ).first()
                refused.append({
                    "id": summary_id,
                    "entry_no": getattr(summary, "entry_no", ""),
                    "doc_num": getattr(summary, "sap_invoice_doc_num", ""),
                    "reason": str(exc),
                })
        if approved:
            transaction.on_commit(
                lambda: notify_bill_summary_decided(approved, approved=True)
            )
        return approved, refused

    @transaction.atomic
    def _approve_one(self, summary_id: int, dispatch_date) -> BillSummary:
        summary = self._for_update(summary_id)
        if summary.status != BillSummaryStatus.PENDING_APPROVAL:
            raise BillSummaryError(
                f"{summary.entry_no} is not waiting for approval "
                f"({summary.get_status_display().lower()})."
            )
        # Demanded here rather than when the sheet was raised: this is the
        # request SAP refuses without one, and until now there was nothing to
        # refuse. See the module docstring for what SAP does instead.
        if not (summary.bilty_no or "").strip():
            raise BillSummaryError(
                f"{summary.entry_no} has no bilty number, and SAP will not accept "
                "a dispatch date without one. Send it back for the bilty."
            )
        self._check_dispatch_date(dispatch_date, summary.invoice_date)

        summary.dispatch_date = dispatch_date
        summary.status = BillSummaryStatus.APPROVED
        summary.approved_by = self.user
        summary.approved_at = timezone.now()
        summary.save(
            update_fields=[
                "dispatch_date", "status", "approved_by", "approved_at", "updated_at",
            ]
        )
        # Outside the approval's own correctness: the warehouse has made its
        # decision, and SAP refusing the stamp is a thing to retry rather than a
        # reason to make the warehouse decide again.
        transaction.on_commit(lambda: self.post_to_sap(summary.id))
        logger.info(
            "Bill summary %s approved for %s", summary.entry_no, dispatch_date
        )
        return summary

    @transaction.atomic
    def reject(self, summary_id: int, reason: str) -> BillSummary:
        """Hand the sheet back to the dispatch desk, with what is wrong with it.

        Costs nothing to undo because nothing has been done: an unapproved sheet
        has never been near SAP.
        """
        if not (reason or "").strip():
            raise BillSummaryError("Say what needs fixing before sending it back.")
        summary = self._for_update(summary_id)
        if summary.status != BillSummaryStatus.PENDING_APPROVAL:
            raise BillSummaryError(
                f"{summary.entry_no} is not waiting for approval "
                f"({summary.get_status_display().lower()})."
            )
        summary.status = BillSummaryStatus.REJECTED
        summary.reject_reason = reason.strip()
        summary.rejected_by = self.user
        summary.rejected_at = timezone.now()
        summary.save(
            update_fields=[
                "status", "reject_reason", "rejected_by", "rejected_at", "updated_at",
            ]
        )
        transaction.on_commit(
            lambda: notify_bill_summary_decided([summary], approved=False)
        )
        return summary

    @transaction.atomic
    def resubmit(self, summary_id: int, data: dict) -> BillSummary:
        """Correct a sheet the warehouse has not approved, and send it again.

        Works on a sheet that was sent back and on one still sitting in the
        queue: in both cases the warehouse has not committed to anything, so
        there is nothing to protect. The lines can be restated too — a short
        dispatch is usually what the sending-back was about.
        """
        summary = self._for_update(summary_id)
        if not summary.is_editable:
            raise BillSummaryError(
                f"{summary.entry_no} is {summary.get_status_display().lower()} "
                "and can no longer be changed."
            )

        for field in (
            "bilty_no", "transporter_name", "vehicle_no", "driver_name", "driver_mobile",
        ):
            if field in data:
                setattr(summary, field, (data.get(field) or "").strip())
        if "bilty_date" in data:
            summary.bilty_date = data.get("bilty_date")
        if "remarks" in data:
            summary.remarks = data.get("remarks") or ""

        if data.get("lines"):
            self._restate_lines(summary, data["lines"])

        summary.status = BillSummaryStatus.PENDING_APPROVAL
        summary.submitted_at = timezone.now()
        summary.save()
        transaction.on_commit(lambda: notify_bill_summary_submitted([summary]))
        return summary

    def _restate_lines(self, summary: BillSummary, rows: list) -> None:
        """Change what is being dispatched on lines the sheet already holds.

        Re-split rather than only re-numbered: the boxes and loose pieces on the
        sheet are what the picker reads, and leaving them showing the old
        quantity's packing would send somebody for the wrong number of cartons.
        """
        by_line = {line.sap_line_num: line for line in summary.active_lines}
        for row in rows:
            number = row.get("sap_line_num")
            if number is None:
                continue
            line = by_line.get(int(number))
            if line is None:
                raise BillSummaryError(
                    f"{summary.entry_no} has no line {number}."
                )
            dispatch_qty = _dec(row.get("dispatch_qty"), "Dispatch quantity")
            if dispatch_qty < 0:
                raise BillSummaryError(
                    f"Dispatch quantity for {line.item_code} cannot be negative."
                )
            if dispatch_qty > line.invoice_qty:
                raise BillSummaryError(
                    f"Cannot dispatch {dispatch_qty} of {line.item_code}: the bill "
                    f"is only for {line.invoice_qty}."
                )
            packing = split_with_pieces_per_box(dispatch_qty, line.pcs_per_box)
            line.dispatch_qty = dispatch_qty
            line.boxes = packing.boxes
            line.loose_qty = packing.loose
            line.save(
                update_fields=["dispatch_qty", "boxes", "loose_qty", "updated_at"]
            )
        if not any(line.dispatch_qty > 0 for line in summary.active_lines):
            raise BillSummaryError(
                "Every line is zero, so SAP would refuse this. Set at least one "
                "dispatch quantity."
            )

    # ------------------------------------------------------------------
    # printed / picked / cancel
    # ------------------------------------------------------------------

    @transaction.atomic
    def mark_printed(self, summary_id: int) -> BillSummary:
        """The dispatch desk has printed the approved sheet to sign it.

        Recorded once. A reprint is a reprint; what the record is for is knowing
        when the signed paper went down to the godown, and that happened the
        first time.
        """
        summary = self._for_update(summary_id)
        if summary.status == BillSummaryStatus.APPROVED:
            summary.status = BillSummaryStatus.PRINTED
        elif summary.status not in (
            BillSummaryStatus.PRINTED, BillSummaryStatus.PICKED
        ):
            raise BillSummaryError(
                f"{summary.entry_no} is {summary.get_status_display().lower()}; "
                "only an approved sheet can be printed for signing."
            )
        if summary.printed_at is None:
            summary.printed_by = self.user
            summary.printed_at = timezone.now()
        summary.save(
            update_fields=["status", "printed_by", "printed_at", "updated_at"]
        )
        return summary

    @transaction.atomic
    def mark_picked(self, summary_id: int) -> BillSummary:
        """The floor has fetched the goods. A record of who and when, no more.

        Allowed on an approved sheet as well as a printed one. The print is the
        dispatch desk's step, and a godown that has the signed paper in its hand
        should not be told it cannot record the pick because nobody pressed a
        button upstairs.
        """
        summary = self._for_update(summary_id)
        if summary.status not in (
            BillSummaryStatus.APPROVED, BillSummaryStatus.PRINTED
        ):
            raise BillSummaryError(
                f"{summary.entry_no} is {summary.get_status_display().lower()}."
            )
        summary.status = BillSummaryStatus.PICKED
        summary.picked_by = self.user
        summary.picked_at = timezone.now()
        summary.save(update_fields=["status", "picked_by", "picked_at", "updated_at"])
        return summary

    def _for_update(self, summary_id: int) -> BillSummary:
        summary = (
            BillSummary.objects.select_for_update()
            .filter(pk=summary_id, company=self.company, is_active=True)
            .first()
        )
        if summary is None:
            raise BillSummaryError("Bill summary not found.")
        return summary

    @transaction.atomic
    def cancel(self, summary_id: int, reason: str) -> BillSummary:
        summary = (
            BillSummary.objects.select_for_update()
            .filter(pk=summary_id, company=self.company, is_active=True)
            .first()
        )
        if summary is None:
            raise BillSummaryError("Bill summary not found.")
        if summary.status == BillSummaryStatus.CANCELLED:
            raise BillSummaryError(f"{summary.entry_no} is already cancelled.")
        if not (reason or "").strip():
            raise BillSummaryError("A cancellation needs a reason.")

        was_posted = summary.sap_status == BillSummarySapStatus.POSTED
        summary.status = BillSummaryStatus.CANCELLED
        summary.cancel_reason = reason.strip()
        summary.save(update_fields=["status", "cancel_reason", "updated_at"])

        # Take the stamp back off the invoice. Outside the cancellation's own
        # correctness: if SAP refuses, the sheet is still cancelled and the
        # failure is recorded for retry, rather than the floor being unable to
        # withdraw a sheet because SAP was unreachable.
        if was_posted:
            transaction.on_commit(lambda: self.post_to_sap(summary.id))
        return summary
