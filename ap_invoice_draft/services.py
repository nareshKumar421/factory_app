"""A/P invoice drafts: make the entry and put the draft into SAP.

The order matters for what the user is left holding when something fails:

1. The entry is saved first, with the bill, so nothing typed or uploaded is lost.
2. The SAP draft is made in the same request. SAP refusing it, or not answering,
   leaves the entry FAILED with SAP's reason and a "Create in SAP" retry. A
   retry, like the first try, looks for an open A/P draft on the GRPO before
   making one: accounts make them by hand too, and a request that timed out may
   have been taken.
"""

import json
import logging
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from sap_client.client import SAPClient
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPOutcomeUnknown,
    SAPValidationError,
)

from .models import APInvoiceDraft, SapDraftStatus
from .sap_reader import GRPOReader

logger = logging.getLogger(__name__)

#: Largest bill accepted.
MAX_INVOICE_BYTES = 15 * 1024 * 1024
#: What the bill may be: a scan as PDF, or a photo.
BILL_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".webp")
SAP_COMMENTS_MAX_LENGTH = 254

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

    def open_grpos(self, search: str = "") -> list:
        """The picker: open material GRPOs, each saying whether the app or SAP
        already has an A/P draft for it."""
        rows = GRPOReader(self.company_code).open_grpos(search)
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

    def list_entries(self, company_ids, search: Optional[str] = None):
        qs = (
            APInvoiceDraft.objects.filter(company_id__in=company_ids, is_active=True)
            .select_related("company", "created_by")
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
        """Save the entry and make its SAP draft. Raises ``ValueError`` for
        anything the user has to change first."""
        if not getattr(invoice_file, "name", "").lower().endswith(BILL_EXTENSIONS):
            raise ValueError("Upload the bill as a PDF or a photo (JPG, PNG).")
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
            client = SAPClient(self.company_code)
            attachment_entry = self._attach_bill(entry, client)
            result = client.create_ap_invoice_draft(
                self._draft_payload(entry, grpo, user, attachment_entry, series)
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
        return self._mark_created(entry, int(draft_entry), adopted=False)

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
    ) -> dict:
        """What accounts' own "Copy To A/P Invoice" makes: the GRPO's open lines,
        its dates, branch and bill number, in the month's series for that branch."""
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
                {"BaseType": 20, "BaseEntry": grpo["doc_entry"], "BaseLine": line["line_num"]}
                for line in grpo["lines"]
                if line["is_open"]
            ],
        }
        if grpo["branch_id"] is not None:
            payload["BPL_IDAssignedToInvoice"] = grpo["branch_id"]
        if series:
            payload["Series"] = series[0]
        if grpo.get("gst_type") in GST_TRANSACTION_TYPES:
            payload["GSTTransactionType"] = GST_TRANSACTION_TYPES[grpo["gst_type"]]
        if attachment_entry:
            payload["AttachmentEntry"] = attachment_entry
        return payload

    def _mark_created(self, entry, draft_entry: int, *, adopted: bool) -> APInvoiceDraft:
        entry.sap_status = SapDraftStatus.CREATED
        entry.sap_draft_entry = draft_entry
        entry.sap_draft_adopted = adopted
        entry.sap_error = ""
        entry.sap_created_at = timezone.now()
        entry.save(update_fields=[
            "sap_status", "sap_draft_entry", "sap_draft_adopted", "sap_error",
            "sap_created_at", "updated_at",
        ])
        return entry

    def _mark_failed(self, entry, message: str) -> APInvoiceDraft:
        logger.warning("ap_invoice_draft %s: %s", entry.entry_no, message)
        entry.sap_status = SapDraftStatus.FAILED
        entry.sap_error = message[:2000]
        entry.save(update_fields=["sap_status", "sap_error", "updated_at"])
        return entry
