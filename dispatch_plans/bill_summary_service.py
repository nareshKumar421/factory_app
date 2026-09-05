"""Generating a bill summary for one bill, and posting it to SAP.

The user searches a bill number; the app fills in everything the dispatch module
already knows about that bill; the user supplies the rest — in practice the
bilty, which is raised once the truck is loaded and so is not yet known when the
sheet is produced; the summary is then posted to SAP and printed for the floor.

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
from django.db import transaction
from django.utils import timezone

from company.models import Company
from gate_core.services.box_packing import split_line
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .hana_reader import HanaDispatchBillReader
from .models import DispatchPlan
from .models_bill_summary import (
    BillSummary,
    BillSummaryLine,
    BillSummarySapStatus,
    BillSummaryStatus,
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

        existing = (
            BillSummary.objects.filter(
                company=self.company, sap_invoice_doc_entry=doc_entry, is_active=True
            )
            .exclude(status=BillSummaryStatus.CANCELLED)
            .first()
        )

        prefill = self._prefill(plan, lines)
        # The bilty is the usual gap, and it is the one SAP will not do without.
        # The dispatch date is not listed here: it is never prefilled, so naming
        # it as "missing from the plan" would only be noise on a field that is
        # always typed.
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

        The dispatch date is deliberately NOT among them, from any source. It is
        the field the whole sheet turns on, it is written into SAP where it can
        never be changed again, and a plan's date is a plan — often days old and
        routinely wrong by the time the truck is actually loaded. An offered date
        gets accepted without being read; this one is typed every time.
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
    # generate
    # ------------------------------------------------------------------

    @transaction.atomic
    def generate(self, data: dict) -> BillSummary:
        """Create the sheet for one bill and post it to SAP.

        The SAP posting is attempted here but its failure does not roll the sheet
        back: the manager still needs something to hand the floor, and a refused
        posting is a thing to retry rather than a reason to lose the document.
        """
        doc_entry = data.get("sap_invoice_doc_entry")
        if not doc_entry:
            raise BillSummaryError("Which bill is this summary for?")

        dispatch_date = data.get("dispatch_date")
        if not dispatch_date:
            raise BillSummaryError("A dispatch date is required.")

        bilty_no = (data.get("bilty_no") or "").strip()
        if not bilty_no:
            # Stated as a rule of SAP's, not ours, because that is what it is.
            raise BillSummaryError(
                "A bilty number is required: SAP will not accept a dispatch date "
                "without one."
            )

        clash = (
            BillSummary.objects.filter(
                company=self.company, sap_invoice_doc_entry=doc_entry, is_active=True
            )
            .exclude(status=BillSummaryStatus.CANCELLED)
            .first()
        )
        if clash:
            raise BillSummaryError(
                f"{clash.entry_no} already covers this bill. Cancel it first to reissue."
            )

        bill = self.reader.get_bill_by_number(str(data.get("sap_invoice_doc_num") or ""))
        lines = self.reader.list_pickable_lines([doc_entry])
        if not lines:
            raise BillSummaryError("That bill has no lines to fetch.")

        doc_date = (bill or {}).get("doc_date")
        self._check_dispatch_date(dispatch_date, doc_date)

        header = bill or {}
        summary = BillSummary.objects.create(
            company=self.company,
            entry_no=BillSummary.generate_entry_no(dispatch_date),
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(data.get("sap_invoice_doc_num") or lines[0]["doc_num"]),
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
            dispatch_date=dispatch_date,
            bilty_no=bilty_no,
            bilty_date=data.get("bilty_date"),
            transporter_name=(data.get("transporter_name") or "").strip(),
            vehicle_no=(data.get("vehicle_no") or "").strip(),
            driver_name=(data.get("driver_name") or "").strip(),
            driver_mobile=(data.get("driver_mobile") or "").strip(),
            remarks=data.get("remarks") or "",
            issued_by=self.user,
        )

        overrides = {
            int(row["sap_line_num"]): _dec(row.get("dispatch_qty"), "Dispatch quantity")
            for row in (data.get("lines") or [])
            if row.get("sap_line_num") is not None
        }
        objects = []
        for line in lines:
            dispatch_qty = overrides.get(line["line_num"], line["quantity"])
            # SAP's own split: full boxes plus leftover pieces, with SalFactor2=1
            # meaning "not boxed at all" (CSD excepted). Never quantity/per-box,
            # which would print a fraction of a carton.
            packing = split_line(dispatch_qty, line.get("sal_factor2"), line["item_name"])
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
        BillSummaryLine.objects.bulk_create(objects)

        if not any(obj.dispatch_qty > 0 for obj in objects):
            raise BillSummaryError(
                "Every line is zero, so SAP would refuse this. Set at least one "
                "dispatch quantity."
            )

        # Outside the record's own correctness — see the docstring.
        transaction.on_commit(lambda: self.post_to_sap(summary.id))
        logger.info("Bill summary %s generated for bill %s",
                    summary.entry_no, summary.sap_invoice_doc_num)
        return summary

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

        kept = []
        try:
            kept = self._patch_invoice(summary, clear=clearing)
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
        # SAP kept its own values for these, and they cannot be changed. Said on
        # the sheet rather than only in the log: the driver is carrying a
        # document that disagrees with the invoice.
        kept = list(kept or [])
        summary.sap_note = (
            "SAP keeps its existing " + ", ".join(kept) + " on this bill; once set, "
            "these cannot be changed."
            if kept else ""
        )
        summary.save(
            update_fields=[
                "sap_status", "sap_error", "sap_posted_at", "sap_note", "updated_at",
            ]
        )
        return summary

    def _patch_invoice(self, summary: BillSummary, *, clear: bool = False) -> list:
        """The write itself. See the module docstring for why it looks like this.

        Returns whatever SAP is keeping in place of the sheet's own values, so
        the sheet can say so instead of implying the invoice matches it.

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
            return []

        payload, kept = self._stamp_payload(
            summary,
            self.reader.dispatch_stamp_columns(),
            self.reader.invoice_dispatch_stamp(summary.sap_invoice_doc_entry),
        )

        response = session.patch(
            f"{sl['base_url']}/b1s/v2/Invoices({summary.sap_invoice_doc_entry})",
            json=payload,
            timeout=180,
        )
        if response.status_code not in (200, 204):
            raise SAPDataError(self._sap_message(response))
        return kept

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

    def _stamp_payload(self, summary: BillSummary, columns: dict, existing: dict):
        """The PATCH body, and what SAP is keeping instead of the sheet's version.

        Anything SAP already holds is left alone rather than overwritten: it
        cannot be changed, and trying is what fails the posting. Where its value
        differs from the sheet's, that is reported back so the difference between
        the printed sheet and the invoice is visible rather than silent.
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

        kept = []
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
            payload[column] = (
                value.strftime("%Y-%m-%d") if field in ("bilty_date",) else value
            )

        return payload, kept

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
    # picked / cancel
    # ------------------------------------------------------------------

    @transaction.atomic
    def mark_picked(self, summary_id: int) -> BillSummary:
        """The floor has fetched the goods. A record of who and when, no more."""
        summary = (
            BillSummary.objects.select_for_update()
            .filter(pk=summary_id, company=self.company, is_active=True)
            .first()
        )
        if summary is None:
            raise BillSummaryError("Bill summary not found.")
        if summary.status != BillSummaryStatus.GENERATED:
            raise BillSummaryError(
                f"{summary.entry_no} is {summary.get_status_display().lower()}."
            )
        summary.status = BillSummaryStatus.PICKED
        summary.picked_by = self.user
        summary.picked_at = timezone.now()
        summary.save(update_fields=["status", "picked_by", "picked_at", "updated_at"])
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
