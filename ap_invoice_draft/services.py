"""A/P invoice drafts: make the entry, put the draft into SAP, read the bill, audit.

The order matters for what the user is left holding when something fails:

1. The entry is saved first, with the bill, so nothing typed or uploaded is lost.
2. The SAP draft is made in the same request. SAP refusing it, or not answering,
   leaves the entry FAILED with SAP's reason and a "Create in SAP" retry. A
   retry, like the first try, looks for an open A/P draft on the GRPO before
   making one: accounts make them by hand too, and a request that timed out may
   have been taken.
3. The bill is read in the same request too, on this server (``invoice_reader``,
   about five seconds a page). A read that fails is kept as FAILED and offered
   again; "Read the bill again" also re-reads after the reader improves.
4. The checks run after each of those, re-reading the GRPO from SAP.
"""

import json
import logging
import re
from decimal import Decimal
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from grpo.models import GRPOPosting, GRPOStatus
from grpo.po_print_settings import apply_to_payload
from raw_material_gatein.services.validations import (
    is_over_receipt_enforced,
    is_over_receipt_exempt,
)
from sap_client.client import SAPClient
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPOutcomeUnknown,
    SAPValidationError,
)

from . import checks, tds
from .invoice_reader import InvoiceReadError, mime_type_for, read_invoice
from .models import (
    APInvoiceDraft,
    APInvoiceDraftCheck,
    InvoiceReadStatus,
    ReviewDecision,
    SapDraftStatus,
)
from .sap_reader import GRPOReader

logger = logging.getLogger(__name__)

#: Largest bill accepted.
MAX_INVOICE_BYTES = 15 * 1024 * 1024
#: A read still marked READING after this long is taken to have died.
READING_STALE_AFTER_SECONDS = 180
SAP_COMMENTS_MAX_LENGTH = 254

GATE_ENTRY_IN_COMMENTS = re.compile(r"Gate Entry:\s*(\S+)")

# The draft's GST transaction type, from the GRPO's ``GSTTranTyp``. It has to
# be sent: a Service Layer draft defaults to a bill of supply ("--"), and SAP
# then refuses the GST series ("first define the numbering series", tried on
# TEST 2026-10-08). A bill of supply is left to that default.
GST_TRANSACTION_TYPES = {
    "GA": "gsttrantyp_GSTTaxInvoice",
    "GD": "gsttrantyp_GSTDebitMemo",
}


def sap_message(exc) -> str:
    """SAP's own sentence out of a Service Layer refusal.

    The shared writers read v1's ``error.message.value``; v2 sends
    ``error.message`` as a plain string, so the whole JSON body arrives here.
    """
    text = str(exc)
    try:
        error = json.loads(text).get("error") or {}
    except (ValueError, AttributeError):
        return text
    message = error.get("message")
    if isinstance(message, dict):
        message = message.get("value")
    return str(message or text).strip()


class APInvoiceDraftService:
    def __init__(self, company):
        self.company = company
        self.company_code = company.code

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def open_grpos(self, search: str = "", doc_entry: Optional[int] = None) -> list:
        """The picker: open material GRPOs, each saying whether the app or SAP
        already has an A/P draft for it. ``doc_entry`` asks for one GRPO, as a
        GRPO's own page does when it opens the form on itself."""
        rows = GRPOReader(self.company_code).open_grpos(search, doc_entry=doc_entry)
        taken = dict(
            APInvoiceDraft.objects.filter(
                company=self.company,
                is_active=True,
                grpo_doc_entry__in=[row["doc_entry"] for row in rows],
            ).values_list("grpo_doc_entry", "entry_no")
        )
        for row in rows:
            row["entry_no"] = taken.get(row["doc_entry"], "")
        return rows

    def grpo_ap_status(self, grpo_entries: list) -> dict:
        """Where each GRPO's A/P invoice stands, for the GRPO pages.

        POSTED: SAP has an A/P invoice for all of it (the GRPO is closed).
        PARTIAL: an A/P invoice took some lines; the GRPO is still open.
        DRAFT: no invoice yet, but an A/P draft is waiting in SAP.
        CLOSED: the GRPO was closed or cancelled without an invoice.
        NONE: nothing yet. A GRPO SAP does not have is left out.
        """
        states = GRPOReader(self.company_code).ap_invoice_states(grpo_entries)
        entries = {
            entry.grpo_doc_entry: entry
            for entry in APInvoiceDraft.objects.filter(
                company=self.company, is_active=True, grpo_doc_entry__in=list(states),
            )
        }
        result = {}
        for grpo_entry, state in states.items():
            entry = entries.get(grpo_entry)
            if state["invoices"]:
                status = "PARTIAL" if state["grpo_open"] else "POSTED"
            elif state["draft_entries"] or (entry and entry.sap_draft_entry):
                status = "DRAFT"
            elif state["grpo_cancelled"] or not state["grpo_open"]:
                status = "CLOSED"
            else:
                status = "NONE"
            result[grpo_entry] = {
                "status": status,
                "invoices": state["invoices"],
                "sap_draft_entries": state["draft_entries"],
                "entry": {
                    "id": entry.pk,
                    "entry_no": entry.entry_no,
                    "sap_status": entry.sap_status,
                    "sap_draft_entry": entry.sap_draft_entry,
                } if entry else None,
            }
        return result

    def list_entries(self, company_ids, search: Optional[str] = None):
        qs = (
            APInvoiceDraft.objects.filter(company_id__in=company_ids, is_active=True)
            .select_related("company", "created_by")
            .prefetch_related("checks")
        )
        if search:
            qs = qs.filter(
                Q(entry_no__icontains=search)
                | Q(grpo_doc_num__icontains=search)
                | Q(grpo_reference__icontains=search)
                | Q(vendor_name__icontains=search)
                | Q(vendor_code__icontains=search)
            )
        return qs

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(self, grpo_doc_entry: int, invoice_file, user) -> APInvoiceDraft:
        """Save the entry, make its SAP draft, run the checks. Raises
        ``ValueError`` for anything the user has to change first."""
        try:
            mime_type_for(getattr(invoice_file, "name", ""))
        except InvoiceReadError as exc:
            raise ValueError(str(exc)) from exc
        if invoice_file.size > MAX_INVOICE_BYTES:
            raise ValueError("The bill is larger than 15 MB. Scan it at a lower resolution.")

        grpo = GRPOReader(self.company_code).grpo(grpo_doc_entry)
        if grpo is None:
            raise ValueError(f"SAP has no GRPO with DocEntry {grpo_doc_entry}.")
        if grpo["is_cancelled"]:
            raise ValueError(f"GRPO {grpo['doc_num']} is cancelled.")
        if grpo["is_service"]:
            raise ValueError(
                f"GRPO {grpo['doc_num']} is a service GRPO; transporter bills go through dispatch."
            )
        if not grpo["is_open"]:
            raise ValueError(f"GRPO {grpo['doc_num']} is already invoiced in SAP.")

        existing = APInvoiceDraft.objects.filter(
            company=self.company, grpo_doc_entry=grpo["doc_entry"], is_active=True,
        ).first()
        if existing:
            raise ValueError(f"GRPO {grpo['doc_num']} already has entry {existing.entry_no}.")

        try:
            with transaction.atomic():
                entry = self._new_entry(grpo, invoice_file, user)
        except IntegrityError as exc:
            # Someone else entered the same GRPO a moment ago.
            raise ValueError(f"GRPO {grpo['doc_num']} already has an entry.") from exc

        self.send_to_sap(entry, user, grpo=grpo)
        self._read(entry)
        self.run_checks(entry, grpo=grpo)
        return entry

    def _new_entry(self, grpo: dict, invoice_file, user) -> APInvoiceDraft:
        return APInvoiceDraft.objects.create(
            company=self.company,
            entry_no=APInvoiceDraft.generate_entry_no(),
            grpo_doc_entry=grpo["doc_entry"],
            grpo_doc_num=grpo["doc_num"],
            grpo_date=grpo["doc_date"],
            grpo_reference=grpo["reference"],
            vendor_code=grpo["vendor_code"],
            vendor_name=grpo["vendor_name"],
            grpo_total=grpo["total"],
            grpo_posting=self._app_grpo(grpo["doc_entry"]),
            invoice_file=invoice_file,
            invoice_filename=getattr(invoice_file, "name", "")[:255],
            created_by=user,
            updated_by=user,
        )

    # ------------------------------------------------------------------
    # SAP draft
    # ------------------------------------------------------------------

    def send_to_sap(self, entry: APInvoiceDraft, user, grpo: Optional[dict] = None) -> APInvoiceDraft:
        """Make the entry's A/P draft in SAP, or link the one SAP already has.

        Never raises for SAP's sake: the outcome is written on the entry.
        """
        if entry.sap_draft_entry:
            return entry
        reader = GRPOReader(self.company_code)
        try:
            found = reader.open_ap_drafts([entry.grpo_doc_entry]).get(entry.grpo_doc_entry)
            if found:
                return self._mark_created(entry, found[0], adopted=True)

            grpo = grpo or reader.grpo(entry.grpo_doc_entry)
            if grpo is None or not grpo["is_open"]:
                return self._mark_failed(
                    entry, "SAP no longer has this GRPO open; it may have been invoiced already."
                )
            series = None
            if grpo["branch_id"] is not None:
                series = reader.ap_invoice_series(grpo["doc_date"], grpo["branch_id"], grpo["gst_type"])
                if series is None:
                    return self._mark_failed(
                        entry,
                        f"SAP has no open A/P invoice series for branch {grpo['branch_id']} on "
                        f"{grpo['doc_date']:%d %b %Y} ({grpo['gst_type']}). Ask the SAP team to open one.",
                    )
            withholding = self._goods_tds(reader, grpo)
            client = SAPClient(self.company_code)
            attachment_entry = self._attach_bill(entry, client)
            result = client.create_ap_invoice_draft(
                self._draft_payload(entry, grpo, user, attachment_entry, series, withholding)
            )
        except SAPValidationError as exc:
            return self._mark_failed(entry, f"SAP refused the draft: {sap_message(exc)}")
        except SAPOutcomeUnknown:
            return self._mark_failed(
                entry,
                "SAP did not answer in time. Try again: the app looks for the draft in SAP "
                "before it makes another.",
            )
        except (SAPConnectionError, SAPDataError) as exc:
            return self._mark_failed(entry, f"SAP is not answering: {exc}")

        draft_entry = result.get("DocEntry")
        if not draft_entry:
            return self._mark_failed(entry, "SAP answered without a draft number.")
        return self._mark_created(
            entry, int(draft_entry), adopted=False, withholding=withholding,
            tds_amount=result.get("WTAmount"),
        )

    @staticmethod
    def _goods_tds(reader: GRPOReader, grpo: dict) -> tds.TdsDecision:
        """The TDS accounts' own copy would put on the GRPO's open lines."""
        bill = sum((line["line_total"] for line in grpo["lines"] if line["is_open"]), Decimal("0"))
        setup = reader.goods_tds(
            grpo["vendor_code"], tds.GOODS_TDS_CODE, *tds.financial_year(grpo["doc_date"]),
        )
        return tds.decide(setup, bill)

    def _attach_bill(self, entry: APInvoiceDraft, client: SAPClient) -> Optional[int]:
        """Upload the bill to SAP's attachments. A failure is noted, not fatal:
        the draft is still worth having, and accounts can attach by hand."""
        if entry.sap_attachment_entry:
            return entry.sap_attachment_entry
        try:
            result = client.upload_attachment(
                file_path=entry.invoice_file.path,
                filename=entry.invoice_filename or entry.invoice_file.name.rsplit("/", 1)[-1],
            )
            absolute_entry = result.get("AbsoluteEntry")
            if not absolute_entry:
                raise SAPDataError("SAP did not return an attachment number.")
        except (SAPValidationError, SAPConnectionError, SAPDataError, OSError) as exc:
            logger.warning("ap_invoice_draft %s: attaching the bill failed: %s", entry.entry_no, exc)
            entry.sap_attachment_error = str(exc)[:1000]
            entry.save(update_fields=["sap_attachment_error", "updated_at"])
            return None
        entry.sap_attachment_entry = int(absolute_entry)
        entry.sap_attachment_error = ""
        entry.save(update_fields=["sap_attachment_entry", "sap_attachment_error", "updated_at"])
        return entry.sap_attachment_entry

    def _draft_payload(
        self, entry, grpo: dict, user, attachment_entry: Optional[int], series=None,
        withholding: Optional[tds.TdsDecision] = None,
    ) -> dict:
        """What accounts' own "Copy To A/P Invoice" makes: the GRPO's open lines,
        its dates, branch and bill number, in the month's series for that branch,
        with the vendor's TDS."""
        withholding = withholding or tds.TdsDecision()
        username = getattr(user, "email", "") or getattr(user, "username", "") or str(user)
        comments = (
            f"App: FactoryApp v2 | AP Draft: {entry.entry_no} | User: {username} | "
            f"Based On Goods Receipt PO {grpo['doc_num']}."
        )
        payload = {
            "CardCode": grpo["vendor_code"],
            "NumAtCard": grpo["reference"],
            "DocDate": grpo["doc_date"].isoformat(),
            "TaxDate": (grpo["tax_date"] or grpo["doc_date"]).isoformat(),
            "Comments": comments[:SAP_COMMENTS_MAX_LENGTH],
            "DocumentLines": [
                {
                    "BaseType": 20, "BaseEntry": grpo["doc_entry"], "BaseLine": line["line_num"],
                    "WTLiable": "tYES" if withholding.code else "tNO",
                }
                for line in grpo["lines"]
                if line["is_open"]
            ],
        }
        if withholding.code:
            payload["WithholdingTaxDataCollection"] = [{"WTCode": withholding.code}]
        if grpo["branch_id"] is not None:
            payload["BPL_IDAssignedToInvoice"] = grpo["branch_id"]
        if series:
            payload["Series"] = series[0]
        if grpo.get("gst_type") in GST_TRANSACTION_TYPES:
            payload["GSTTransactionType"] = GST_TRANSACTION_TYPES[grpo["gst_type"]]
        if attachment_entry:
            payload["AttachmentEntry"] = attachment_entry
        return payload

    def _mark_created(
        self, entry, draft_entry: int, *, adopted: bool,
        withholding: Optional[tds.TdsDecision] = None, tds_amount=None,
    ) -> APInvoiceDraft:
        withholding = withholding or tds.TdsDecision()
        entry.sap_status = SapDraftStatus.CREATED
        entry.sap_draft_entry = draft_entry
        entry.sap_draft_adopted = adopted
        entry.sap_error = ""
        entry.sap_created_at = timezone.now()
        entry.tds_code = withholding.code
        entry.tds_taxable = withholding.taxable
        entry.tds_amount = Decimal(str(tds_amount)) if withholding.code and tds_amount is not None else None
        entry.tds_note = withholding.note
        entry.save(update_fields=[
            "sap_status", "sap_draft_entry", "sap_draft_adopted", "sap_error",
            "sap_created_at", "tds_code", "tds_taxable", "tds_amount", "tds_note", "updated_at",
        ])
        return entry

    def _mark_failed(self, entry, message: str) -> APInvoiceDraft:
        logger.warning("ap_invoice_draft %s: %s", entry.entry_no, message)
        entry.sap_status = SapDraftStatus.FAILED
        entry.sap_error = message[:2000]
        entry.save(update_fields=["sap_status", "sap_error", "updated_at"])
        return entry

    # ------------------------------------------------------------------
    # Reading the bill
    # ------------------------------------------------------------------

    def read_invoice(self, entry: APInvoiceDraft) -> APInvoiceDraft:
        """Read the bill again and re-run the checks. Raises ``ValueError``
        when a read of the same bill is already under way."""
        with transaction.atomic():
            locked = APInvoiceDraft.objects.select_for_update().get(pk=entry.pk)
            started = locked.updated_at
            if (
                locked.invoice_read_status == InvoiceReadStatus.READING
                and started
                and (timezone.now() - started).total_seconds() < READING_STALE_AFTER_SECONDS
            ):
                raise ValueError("The bill is already being read. Wait a moment.")
            locked.invoice_read_status = InvoiceReadStatus.READING
            locked.save(update_fields=["invoice_read_status", "updated_at"])

        entry.refresh_from_db()
        if self._read(entry):
            self.run_checks(entry)
        return entry

    def _read(self, entry: APInvoiceDraft) -> bool:
        """OCR the bill onto the entry. False, with the reason kept, if it could not."""
        try:
            with entry.invoice_file.open("rb") as handle:
                content = handle.read()
            data, model = read_invoice(content, entry.invoice_filename or entry.invoice_file.name)
        except (InvoiceReadError, OSError) as exc:
            entry.invoice_read_status = InvoiceReadStatus.FAILED
            entry.invoice_read_error = str(exc)
            entry.save(update_fields=["invoice_read_status", "invoice_read_error", "updated_at"])
            return False
        except Exception:
            # The OCR engine is native code; whatever it throws, keep the entry.
            logger.exception("ap_invoice_draft %s: reading the bill failed", entry.entry_no)
            entry.invoice_read_status = InvoiceReadStatus.FAILED
            entry.invoice_read_error = "The bill could not be read. Try again, or check it by eye."
            entry.save(update_fields=["invoice_read_status", "invoice_read_error", "updated_at"])
            return False

        entry.invoice_read_status = InvoiceReadStatus.READ
        entry.invoice_data = data
        entry.invoice_read_error = ""
        entry.invoice_read_model = model
        entry.invoice_read_at = timezone.now()
        entry.save(update_fields=[
            "invoice_read_status", "invoice_data", "invoice_read_error",
            "invoice_read_model", "invoice_read_at", "updated_at",
        ])
        return True

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def run_checks(self, entry: APInvoiceDraft, grpo: Optional[dict] = None) -> APInvoiceDraft:
        """Re-read the GRPO and everything around it, and rewrite the findings.
        A person's decisions on a check are kept."""
        invoice = entry.invoice_data if entry.invoice_read_status == InvoiceReadStatus.READ else None
        try:
            grpo = grpo or GRPOReader(self.company_code).grpo(entry.grpo_doc_entry)
        except (SAPConnectionError, SAPDataError) as exc:
            findings = checks.unknown_findings(f"Could not read the GRPO from SAP: {exc}")
        else:
            if grpo is None:
                findings = checks.unknown_findings("SAP no longer has this GRPO.")
            else:
                findings = checks.run_checks(grpo, invoice, self.gather_context(entry, grpo))

        with transaction.atomic():
            for position, finding in enumerate(findings):
                APInvoiceDraftCheck.objects.update_or_create(
                    draft=entry,
                    key=finding.key,
                    defaults={
                        "position": position,
                        "label": finding.label,
                        "status": finding.status,
                        "detail": finding.detail,
                        "facts": finding.facts,
                    },
                )
            entry.checks_run_at = timezone.now()
            entry.save(update_fields=["checks_run_at", "updated_at"])
        return entry

    def gather_context(self, entry: APInvoiceDraft, grpo: dict) -> dict:
        """Everything the checks need beyond the GRPO and the bill."""
        posting = entry.grpo_posting or self._app_grpo(grpo["doc_entry"])
        if posting and entry.grpo_posting_id != posting.pk:
            entry.grpo_posting = posting
            entry.save(update_fields=["grpo_posting", "updated_at"])
        vehicle_entry = posting.vehicle_entry if posting else self._gate_entry_from_comments(grpo)

        arrival = None
        if vehicle_entry is not None:
            arrival = {
                "at": timezone.localtime(vehicle_entry.entry_time).date(),
                "source": f"gate entry {vehicle_entry.entry_no}",
            }
        return {
            "arrival": arrival,
            "po_approvals": self._po_approvals(grpo),
            "vendor_exempt": self._vendor_exempt(grpo["vendor_code"]),
            "qc_items": self._qc_items(grpo, posting, vehicle_entry),
        }

    def _app_grpo(self, doc_entry: int) -> Optional[GRPOPosting]:
        return (
            GRPOPosting.objects.filter(
                sap_doc_entry=doc_entry,
                vehicle_entry__company=self.company,
                status=GRPOStatus.POSTED,
            )
            .select_related("vehicle_entry")
            .first()
        )

    def _gate_entry_from_comments(self, grpo: dict):
        """A GRPO made in SAP for a truck the app took in names the gate entry
        in its comments, as the app's own GRPOs do."""
        from driver_management.models import VehicleEntry

        match = GATE_ENTRY_IN_COMMENTS.search(grpo.get("comments") or "")
        if not match:
            return None
        return VehicleEntry.objects.filter(company=self.company, entry_no=match.group(1)).first()

    def _po_approvals(self, grpo: dict) -> Optional[list]:
        """Each base PO's approval as its printed sheet shows it."""
        pos = {}
        for line in grpo["lines"]:
            if line["po_doc_entry"]:
                pos.setdefault(line["po_doc_entry"], line["po_num"])
        client = SAPClient(self.company_code)
        approvals = []
        try:
            for doc_entry, po_num in pos.items():
                payload = client.po_print(doc_entry)
                if payload is None:
                    approvals.append({"po_num": po_num, "is_approved": False, "approver": ""})
                    continue
                approval = apply_to_payload(payload, self.company).get("approval") or {}
                approvals.append({
                    "po_num": str(payload.get("doc_num") or po_num),
                    "is_approved": bool(approval.get("is_approved")),
                    "approver": approval.get("approver") or "",
                })
        except (SAPConnectionError, SAPDataError) as exc:
            logger.warning("ap_invoice_draft: PO print read failed: %s", exc)
            return None
        return approvals

    def _vendor_exempt(self, vendor_code: str) -> bool:
        """Exempt as the gate exempts: the vendor list and SAP's branch vendors.
        A company that does not enforce the limit at the gate is still audited."""
        if not is_over_receipt_enforced(self.company_code):
            return False
        return is_over_receipt_exempt(self.company_code, vendor_code)

    def _qc_items(self, grpo: dict, posting, vehicle_entry) -> Optional[list]:
        """QC on each of the truck's PO items this GRPO took, or None when the
        truck did not come through the app."""
        from grpo.services import GRPOService

        if vehicle_entry is None:
            return None
        po_numbers = {line["po_num"] for line in grpo["lines"] if line["po_num"]}
        if posting is not None and posting.lines.exists():
            items = [line.po_item_receipt for line in posting.lines.select_related(
                "po_item_receipt__po_receipt"
            )]
        else:
            items = [
                item
                for receipt in vehicle_entry.po_receipts.prefetch_related("items")
                if not po_numbers or receipt.po_number in po_numbers
                for item in receipt.items.all()
            ]
        service = GRPOService(company_code=self.company_code)
        result = []
        for item in items:
            status, _slip, inspection = service._get_item_qc_summary(item)
            result.append({
                "po_num": item.po_receipt.po_number,
                "item_code": item.po_item_code,
                "status": str(status or "PENDING"),
                "report_no": inspection.report_no if inspection else "",
            })
        return result

    # ------------------------------------------------------------------
    # A person's decision on a check
    # ------------------------------------------------------------------

    def review_check(self, entry: APInvoiceDraft, key: str, decision: str, remark: str, user):
        check = entry.checks.filter(key=key).first()
        if check is None:
            raise ValueError("No such check on this entry.")
        if decision and decision not in ReviewDecision.values:
            raise ValueError("Mark the check OK or Not OK.")
        if decision == ReviewDecision.NOT_OK and not (remark or "").strip():
            raise ValueError("Say why it is not OK.")
        check.review_decision = decision or ""
        check.review_remark = (remark or "").strip() if decision else ""
        check.reviewed_by = user if decision else None
        check.reviewed_at = timezone.now() if decision else None
        check.save(update_fields=["review_decision", "review_remark", "reviewed_by", "reviewed_at"])
        return check
