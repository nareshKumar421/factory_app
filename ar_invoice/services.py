"""Coordinates the A/R invoice lifecycle against SAP.

Flow: pick a customer's open Sales Order lines → submit (local PENDING record)
→ post to SAP (``Invoices``, lines copied by ``BaseType 17``). The Service
Layer login is expected to be an originator on an active ObjType-13 approval
template, so the post normally comes back as ``pending_approval`` with an ODRF
draft; the record tracks the draft's approval request (the same requests the
warehouse Invoice Approval page decides) until it is approved and the draft is
added as the real OINV invoice. When no template matches, SAP posts directly
and the record jumps straight to POSTED.

A/R specifics vs the A/P twin (``ap_invoice.services``):

* Base lines come from open SO quantity (``RDR1.OpenQty``); copying a base line
  without a Quantity invoices exactly the open quantity.
* A/R drafts carry NO batch allocations — ``post_approved_draft`` FIFO-allocates
  batches per batch-managed line (the same allocator the stock-transfer flow
  uses) and writes them onto the draft before adding it. A shortfall surfaces
  as a validation error and the record stays APPROVED for retry after stock is
  corrected.
* A draft pending approval does not reduce ``RDR1.OpenQty``, so lines claimed
  by our own live records are filtered app-side.
"""
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone

from company.models import Company
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.hana.batch_stock_reader import InsufficientBatchStock

from .models import (
    ARInvoiceAttachment,
    ARInvoiceLine,
    ARInvoicePosting,
    ARInvoiceStatus,
)

logger = logging.getLogger(__name__)

# Statuses under which a record still lays claim to its SO lines. REJECTED
# releases them; POSTED lines are closed by SAP itself (OpenQty drops).
ACTIVE_STATUSES = (
    ARInvoiceStatus.PENDING,
    ARInvoiceStatus.PENDING_APPROVAL,
    ARInvoiceStatus.APPROVED,
    ARInvoiceStatus.FAILED,
    ARInvoiceStatus.POSTED,
)

SALES_ORDER_OBJECT_TYPE = 17  # ORDR — base document of an SO-copied invoice


class ARInvoiceService:
    def __init__(self, company_code: str):
        self.company_code = company_code
        self.company = Company.objects.get(code=company_code)

    def sap(self) -> SAPClient:
        return SAPClient(company_code=self.company_code)

    # ------------------------------------------------------------------
    # Open lines
    # ------------------------------------------------------------------

    def open_so_lines(
        self, customer_code: str, search: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """SAP's open SO lines for the customer, minus lines our live records claim."""
        rows = self.sap().open_so_lines_for_invoicing(customer_code, search=search)
        if not rows:
            return []

        claimed = set(
            ARInvoiceLine.objects.filter(
                ar_invoice__company=self.company,
                ar_invoice__status__in=ACTIVE_STATUSES,
            ).values_list("base_entry", "base_line")
        )
        return [
            row for row in rows if (row["so_doc_entry"], row["line_num"]) not in claimed
        ]

    # ------------------------------------------------------------------
    # Submit + post
    # ------------------------------------------------------------------

    def create_invoice(
        self,
        user,
        customer_code: str,
        line_keys: List[Dict[str, int]],
        customer_ref: str = "",
        attachments: Optional[list] = None,
        doc_date=None,
        doc_due_date=None,
        tax_date=None,
        comments: str = "",
    ) -> ARInvoicePosting:
        """Record the submission and immediately send it to SAP.

        ``line_keys`` is ``[{"so_doc_entry": int, "line_num": int}, ...]`` —
        full open lines only (SAP copies the open quantity and the SO price).
        """
        customer_code = (customer_code or "").strip()
        if not customer_code:
            raise ValueError("Customer is required.")
        if not line_keys:
            raise ValueError("At least one Sales Order line must be selected.")

        selected = self._resolve_selected_lines(customer_code, line_keys)

        with transaction.atomic():
            posting = ARInvoicePosting.objects.create(
                company=self.company,
                customer_code=customer_code,
                customer_name=selected["customer_name"],
                customer_ref=(customer_ref or "").strip(),
                doc_date=doc_date,
                doc_due_date=doc_due_date,
                tax_date=tax_date,
                selected_total=selected["selected_total"],
                branch_id=selected["branch_id"],
                comments=comments or "",
                status=ARInvoiceStatus.PENDING,
                created_by=user,
                updated_by=user,
            )
            ARInvoiceLine.objects.bulk_create(
                ARInvoiceLine(
                    ar_invoice=posting,
                    base_entry=row["so_doc_entry"],
                    base_line=row["line_num"],
                    base_doc_num=row["so_doc_num"],
                    item_code=row["item_code"],
                    description=row["description"],
                    quantity=row["open_qty"],
                    price=row["price"],
                    line_total=self._decimal(row["open_total"]),
                    tax_code=row["tax_code"],
                    warehouse_code=row["warehouse_code"],
                )
                for row in selected["lines"]
            )
            for uploaded_file in attachments or []:
                ARInvoiceAttachment.objects.create(
                    ar_invoice=posting,
                    file=uploaded_file,
                    original_filename=getattr(uploaded_file, "name", ""),
                    uploaded_by=user,
                )

        return self.post_to_sap(posting.id, user)

    def create_direct_invoice(
        self,
        user,
        customer_code: str,
        direct_lines: List[Dict[str, Any]],
        customer_ref: str = "",
        attachments: Optional[list] = None,
        doc_date=None,
        doc_due_date=None,
        tax_date=None,
        comments: str = "",
    ) -> ARInvoicePosting:
        """A direct (cash/counter) sale: free lines with no base document.

        ``direct_lines`` is ``[{"item_code", "quantity", "unit_price",
        "tax_code", "warehouse_code", "description"?}, ...]``. The same SAP
        journey applies (approval draft, batches, SP rules); only the line
        shape differs — mirroring how the org's real cash-sale invoices look
        (free lines, per-line price/tax/warehouse, Dimension-1 cost centre).
        """
        customer_code = (customer_code or "").strip()
        if not customer_code:
            raise ValueError("Customer is required.")
        if not direct_lines:
            raise ValueError("At least one line is required.")

        sap_client = self.sap()
        customer = sap_client.get_customer(customer_code)
        if customer is None:
            raise ValueError(f"Customer {customer_code} was not found in SAP.")

        warehouses = {(line.get("warehouse_code") or "").strip() for line in direct_lines}
        if "" in warehouses:
            raise ValueError("Every line needs a warehouse.")
        branches = sap_client.get_warehouse_branches()
        branch_ids = {branches.get(whs) for whs in warehouses}
        if None in branch_ids:
            missing = sorted(w for w in warehouses if branches.get(w) is None)
            raise ValueError(f"Unknown warehouse(s): {', '.join(missing)}")
        if len(branch_ids) != 1:
            raise ValueError("All lines must ship from warehouses of one SAP branch.")

        # Dimension-1 profit centre per item — real cash invoices always carry
        # it, and SAP's stored procedure rejects a line without it (as both
        # OcrCode and U_SchemeAgst). Refuse up front rather than posting a
        # document SAP will bounce with an opaque code.
        item_codes = [line["item_code"] for line in direct_lines]
        varieties = sap_client.return_variety_codes(item_codes)
        unmapped = sorted({code for code in item_codes if not varieties.get(code)})
        if unmapped:
            raise ValueError(
                "No SAP variety (Dimension-1) mapping for: "
                f"{', '.join(unmapped)}. Ask the SAP team to set the item's "
                "sub-group before invoicing it."
            )

        selected_total = Decimal("0.00")
        for line in direct_lines:
            quantity = Decimal(str(line["quantity"]))
            unit_price = Decimal(str(line["unit_price"]))
            if quantity <= 0:
                raise ValueError(f"Quantity must be positive ({line['item_code']}).")
            if unit_price < 0:
                raise ValueError(f"Price cannot be negative ({line['item_code']}).")
            if not (line.get("tax_code") or "").strip():
                raise ValueError(f"Tax code is required ({line['item_code']}).")
            selected_total += quantity * unit_price

        with transaction.atomic():
            posting = ARInvoicePosting.objects.create(
                company=self.company,
                customer_code=customer_code,
                customer_name=customer["customer_name"],
                customer_ref=(customer_ref or "").strip(),
                doc_date=doc_date,
                doc_due_date=doc_due_date,
                tax_date=tax_date,
                selected_total=selected_total.quantize(Decimal("0.01")),
                branch_id=int(next(iter(branch_ids))),
                comments=comments or "",
                status=ARInvoiceStatus.PENDING,
                created_by=user,
                updated_by=user,
            )
            ARInvoiceLine.objects.bulk_create(
                ARInvoiceLine(
                    ar_invoice=posting,
                    base_entry=None,
                    base_line=None,
                    item_code=line["item_code"],
                    description=(line.get("description") or "")[:255],
                    quantity=Decimal(str(line["quantity"])),
                    price=Decimal(str(line["unit_price"])),
                    line_total=(
                        Decimal(str(line["quantity"])) * Decimal(str(line["unit_price"]))
                    ).quantize(Decimal("0.01")),
                    tax_code=line["tax_code"].strip(),
                    warehouse_code=line["warehouse_code"].strip(),
                    cost_center=varieties.get(line["item_code"], ""),
                )
                for line in direct_lines
            )
            for uploaded_file in attachments or []:
                ARInvoiceAttachment.objects.create(
                    ar_invoice=posting,
                    file=uploaded_file,
                    original_filename=getattr(uploaded_file, "name", ""),
                    uploaded_by=user,
                )

        return self.post_to_sap(posting.id, user)

    def post_to_sap(self, posting_id: int, user) -> ARInvoicePosting:
        """Send a PENDING/FAILED record to SAP (retriable)."""
        posting = self.get_posting(posting_id)
        if posting.status not in (ARInvoiceStatus.PENDING, ARInvoiceStatus.FAILED):
            raise ValueError("Only pending or failed A/R invoices can be posted to SAP.")

        attachment_records = posting.attachments.all().order_by("id")
        sap_client = self.sap()
        try:
            attachment_entry = self._upload_attachments(sap_client, attachment_records)
            # SAP validates the document IN FULL before diverting it into an
            # approval draft — including batch selection (-4014) — and then
            # discards the allocations when it saves the draft. So the create
            # payload must carry a FIFO allocation for batch-managed lines,
            # and post_approved_draft re-allocates onto the draft at add time.
            batch_allocations = self._allocate_posting_batches(sap_client, posting)
            payload = self._build_payload(posting, attachment_entry, batch_allocations)
            logger.info(
                "A/R invoice payload for %s #%s: %s",
                posting.customer_code, posting.id, payload,
            )
            result = sap_client.create_ar_invoice(payload)
        except (SAPValidationError, SAPConnectionError, SAPDataError) as exc:
            self._mark_failed(posting, str(exc), user)
            raise
        except Exception as exc:
            self._mark_failed(posting, str(exc), user)
            raise SAPDataError(f"Unexpected error: {exc}") from exc

        if result.get("pending_approval"):
            posting.sap_draft_entry = result.get("draft_entry")
            posting.status = ARInvoiceStatus.PENDING_APPROVAL
            posting.error_message = None
            posting.updated_by = user
            # The OWDD request opens in the same SAP transaction as the draft;
            # link it now so the approvals page and this record share an id.
            try:
                state = sap_client.ar_draft_state(posting.sap_draft_entry)
                if state:
                    posting.sap_approval_code = state.get("approval_code")
            except (SAPConnectionError, SAPDataError):
                logger.warning(
                    "Could not read approval request for draft %s; will link on refresh.",
                    posting.sap_draft_entry,
                )
            posting.save(
                update_fields=[
                    "sap_draft_entry", "sap_approval_code", "status",
                    "error_message", "updated_by", "updated_at",
                ]
            )
            attachment_records.update(sap_attachment_status="UPLOADED")
            return posting

        self._mark_posted(
            posting,
            user,
            doc_entry=result.get("DocEntry"),
            doc_num=result.get("DocNum"),
            doc_total=result.get("DocTotal"),
        )
        attachment_records.update(sap_attachment_status="LINKED")
        return posting

    # ------------------------------------------------------------------
    # Approval tracking
    # ------------------------------------------------------------------

    def refresh_from_sap(self, posting_id: int, user) -> ARInvoicePosting:
        """Re-read the draft/approval state for an in-flight record."""
        posting = self.get_posting(posting_id)
        if posting.status not in (
            ARInvoiceStatus.PENDING_APPROVAL,
            ARInvoiceStatus.APPROVED,
        ):
            return posting
        if not posting.sap_draft_entry:
            return posting

        sap_client = self.sap()

        # Posted already? (Ours via post_approved_draft, or added by a B1 user.)
        invoice = sap_client.ar_invoice_for_draft(posting.sap_draft_entry)
        if invoice:
            self._mark_posted(
                posting,
                user,
                doc_entry=invoice["doc_entry"],
                doc_num=invoice["doc_num"],
                doc_total=invoice["doc_total"],
            )
            posting.attachments.all().update(sap_attachment_status="LINKED")
            return posting

        state = sap_client.ar_draft_state(posting.sap_draft_entry)
        if not state:
            posting.error_message = (
                f"Draft {posting.sap_draft_entry} no longer exists in SAP."
            )
            posting.status = ARInvoiceStatus.FAILED
            posting.updated_by = user
            posting.save(
                update_fields=["error_message", "status", "updated_by", "updated_at"]
            )
            return posting

        update_fields = ["updated_by", "updated_at"]
        if state.get("approval_code") and state["approval_code"] != posting.sap_approval_code:
            posting.sap_approval_code = state["approval_code"]
            update_fields.append("sap_approval_code")

        approval_status = state.get("approval_status")
        if approval_status == "Y" and posting.status != ARInvoiceStatus.APPROVED:
            posting.status = ARInvoiceStatus.APPROVED
            update_fields.append("status")
        elif approval_status == "N":
            posting.status = ARInvoiceStatus.REJECTED
            posting.approval_remarks = state.get("reject_remarks") or ""
            update_fields.extend(["status", "approval_remarks"])

        posting.updated_by = user
        posting.save(update_fields=update_fields)
        return posting

    def post_approved_draft(self, posting_id: int, user) -> ARInvoicePosting:
        """Turn an approved draft into the real OINV invoice.

        Batch-managed lines get a FIFO batch allocation written onto the draft
        first — an A/R draft carries none, and the add would fail without one.
        """
        posting = self.refresh_from_sap(posting_id, user)
        if posting.status == ARInvoiceStatus.POSTED:
            return posting
        if posting.status != ARInvoiceStatus.APPROVED:
            raise ValueError(
                "Only an approved A/R invoice draft can be posted "
                f"(current status: {posting.get_status_display()})."
            )

        sap_client = self.sap()
        try:
            self._allocate_draft_batches(sap_client, posting)
            sap_client.save_ar_draft_to_document(posting.sap_draft_entry)
        except (SAPValidationError, SAPConnectionError, SAPDataError) as exc:
            # Keep APPROVED: the approval stands, only the add failed (batch
            # shortfall, an SBO_SP_TransactionNotification rule, …). Retriable.
            posting.error_message = str(exc)
            posting.updated_by = user
            posting.save(update_fields=["error_message", "updated_by", "updated_at"])
            raise

        invoice = sap_client.ar_invoice_for_draft(posting.sap_draft_entry)
        self._mark_posted(
            posting,
            user,
            doc_entry=invoice["doc_entry"] if invoice else None,
            doc_num=invoice["doc_num"] if invoice else None,
            doc_total=invoice["doc_total"] if invoice else None,
        )
        posting.attachments.all().update(sap_attachment_status="LINKED")
        return posting

    def cancel(self, posting_id: int, user) -> ARInvoicePosting:
        """Abandon a record that never reached SAP, releasing its SO lines.

        Only PENDING/FAILED can be cancelled — once SAP holds a draft
        (PENDING_APPROVAL onwards) the document exists there and must be
        rejected or handled in SAP instead.
        """
        posting = self.get_posting(posting_id)
        if posting.status not in (ARInvoiceStatus.PENDING, ARInvoiceStatus.FAILED):
            raise ValueError(
                "Only a pending or failed A/R invoice can be cancelled "
                f"(current status: {posting.get_status_display()})."
            )
        posting.status = ARInvoiceStatus.CANCELLED
        posting.updated_by = user
        posting.save(update_fields=["status", "updated_by", "updated_at"])
        return posting

    def find_by_approval_code(self, wdd_code: int) -> Optional[ARInvoicePosting]:
        """The local record behind one SAP approval request, if it is ours."""
        return (
            ARInvoicePosting.objects.filter(
                company=self.company, sap_approval_code=int(wdd_code)
            )
            .order_by("-id")
            .first()
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_history(self):
        return (
            ARInvoicePosting.objects.filter(company=self.company)
            .select_related("company", "posted_by", "created_by")
            .prefetch_related("lines", "attachments")
            .order_by("-created_at")
        )

    def print_payload(self, posting_id: int) -> dict:
        """SAP's own TAX INVOICE, for one posted record.

        Only a posted invoice has a document to print: a record still waiting on
        approval has a draft, and SAP does not number, tax or date a draft the
        way the printed bill needs. The bill is read fresh from SAP every time
        rather than snapshotted here, because the document can still be edited
        in SAP after we post it and the sheet must show what SAP holds now.
        """
        posting = self.get_posting(posting_id)
        if not posting.sap_doc_entry:
            raise ValueError(
                "This invoice has not been posted to SAP yet, so there is nothing to print."
            )

        payload = self.sap().ar_invoice_print(posting.sap_doc_entry)
        if not payload:
            raise ValueError(
                f"SAP has no invoice {posting.sap_doc_num or posting.sap_doc_entry} "
                f"for {self.company.code}."
            )
        payload["posting_id"] = posting.id
        return payload

    def get_posting(self, posting_id: int) -> ARInvoicePosting:
        try:
            return (
                ARInvoicePosting.objects.filter(company=self.company)
                .select_related("company", "posted_by", "created_by")
                .prefetch_related("lines", "attachments")
                .get(id=posting_id)
            )
        except ARInvoicePosting.DoesNotExist as exc:
            raise ValueError("A/R invoice posting not found.") from exc

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _resolve_selected_lines(
        self, customer_code: str, line_keys: List[Dict[str, int]]
    ) -> Dict[str, Any]:
        open_rows = self.open_so_lines(customer_code)
        by_key = {(row["so_doc_entry"], row["line_num"]): row for row in open_rows}

        selected = []
        missing = []
        for key in line_keys:
            k = (int(key["so_doc_entry"]), int(key["line_num"]))
            row = by_key.get(k)
            if row is None:
                missing.append(f"{k[0]}/{k[1]}")
            else:
                selected.append(row)
        if missing:
            raise ValueError(
                "Sales Order line(s) not open for invoicing (closed, fully "
                f"invoiced, or claimed by another submission): {', '.join(missing)}"
            )

        branch_ids = {row["branch_id"] for row in selected}
        if len(branch_ids) != 1 or next(iter(branch_ids)) is None:
            raise ValueError("Selected Sales Order lines must belong to one SAP branch.")

        return {
            "lines": selected,
            "branch_id": int(next(iter(branch_ids))),
            "customer_name": selected[0]["customer_name"],
            "selected_total": sum(
                (self._decimal(row["open_total"]) for row in selected),
                Decimal("0.00"),
            ),
        }

    def _allocate_posting_batches(
        self, sap_client: SAPClient, posting: ARInvoicePosting
    ) -> Dict[int, list]:
        """FIFO batch allocation per batch-managed line of the submission,
        keyed by the local line id (feeds ``_build_payload``)."""
        lines = list(posting.lines.all().order_by("id"))
        flags = sap_client.batch_managed_flags(
            [line.item_code for line in lines if line.item_code]
        )
        allocations: Dict[int, list] = {}
        try:
            for line in lines:
                if not flags.get(line.item_code):
                    continue
                if not line.quantity or not line.warehouse_code:
                    continue
                allocations[line.id] = sap_client.allocate_batches_fifo(
                    line.item_code, line.warehouse_code, line.quantity
                )
        except InsufficientBatchStock as exc:
            # Surface a shortfall as a clear validation error, not a gateway one.
            raise SAPValidationError(str(exc)) from exc
        return allocations

    def _build_payload(
        self,
        posting: ARInvoicePosting,
        attachment_entry: Optional[int],
        batch_allocations: Optional[Dict[int, list]] = None,
    ) -> Dict[str, Any]:
        batch_allocations = batch_allocations or {}
        document_lines = []
        for line in posting.lines.all().order_by("id"):
            if line.base_entry is not None:
                # Copied from a Sales Order — SAP fills item/qty/price/tax.
                line_data: Dict[str, Any] = {
                    "BaseType": SALES_ORDER_OBJECT_TYPE,
                    "BaseEntry": line.base_entry,
                    "BaseLine": line.base_line,
                }
            else:
                # Direct (cash) sale — a free line, fully specified.
                line_data = {
                    "ItemCode": line.item_code,
                    "Quantity": line.quantity,
                    "UnitPrice": line.price,
                    "TaxCode": line.tax_code,
                    "WarehouseCode": line.warehouse_code,
                }
                if line.cost_center:
                    # SBO_SP_TransactionNotification rejects an A/R invoice line
                    # missing EITHER the Dimension-1 variety (1313257) or the
                    # U_SchemeAgst UDF (1310325). Both carry the same code —
                    # they match on 1,702 of 1,709 live lines — so the one
                    # resolved value fills both. SO-copied lines inherit theirs
                    # from the Sales Order.
                    line_data["CostingCode"] = line.cost_center
                    line_data["U_SchemeAgst"] = line.cost_center
            if line.id in batch_allocations:
                line_data["BatchNumbers"] = batch_allocations[line.id]
            document_lines.append(line_data)

        payload = {
            "DocType": "dDocument_Items",
            "CardCode": posting.customer_code,
            "BPL_IDAssignedToInvoice": posting.branch_id,
            "Comments": self._structured_comments(posting),
            "DocumentLines": document_lines,
        }
        if posting.customer_ref:
            payload["NumAtCard"] = posting.customer_ref
        if posting.doc_date:
            payload["DocDate"] = str(posting.doc_date)
        if posting.doc_due_date:
            payload["DocDueDate"] = str(posting.doc_due_date)
        if posting.tax_date:
            payload["TaxDate"] = str(posting.tax_date)
        if attachment_entry:
            payload["AttachmentEntry"] = attachment_entry
        return payload

    def _structured_comments(self, posting: ARInvoicePosting) -> str:
        user = posting.created_by
        full_name = getattr(user, "full_name", "") or ""
        username = getattr(user, "email", "") or str(user)
        parts = [
            "App: FactoryApp v2",
            f"User: {full_name} ({username})" if full_name else f"User: {username}",
            "Document: A/R Invoice",
        ]
        if posting.customer_ref:
            parts.append(f"Customer Ref: {posting.customer_ref}")
        if posting.comments:
            parts.append(posting.comments)
        return " | ".join(parts)

    def _allocate_draft_batches(self, sap_client: SAPClient, posting) -> None:
        """FIFO-allocate batches for the draft's batch-managed lines and write
        them onto the draft. Lines come from the draft itself (DRF1) so the
        LineNums the PATCH addresses are exactly SAP's."""
        draft_lines = sap_client.ar_draft_lines(posting.sap_draft_entry)
        if not draft_lines:
            return
        flags = sap_client.batch_managed_flags(
            [line["item_code"] for line in draft_lines if line["item_code"]]
        )

        patched_lines = []
        try:
            for line in draft_lines:
                if not flags.get(line["item_code"]):
                    continue
                if not line["quantity"] or not line["warehouse_code"]:
                    continue
                allocation = sap_client.allocate_batches_fifo(
                    line["item_code"], line["warehouse_code"], line["quantity"]
                )
                patched_lines.append(
                    {"LineNum": line["line_num"], "BatchNumbers": allocation}
                )
        except InsufficientBatchStock as exc:
            # Surface a shortfall as a clear validation error, not a gateway one.
            raise SAPValidationError(str(exc)) from exc

        if patched_lines:
            sap_client.update_ar_draft(
                posting.sap_draft_entry, {"DocumentLines": patched_lines}
            )

    def _upload_attachments(self, sap_client: SAPClient, attachments) -> Optional[int]:
        absolute_entry = None
        for attachment in attachments:
            if attachment.sap_attachment_status in ("UPLOADED", "LINKED") and (
                attachment.sap_absolute_entry
            ):
                absolute_entry = attachment.sap_absolute_entry
                continue
            try:
                if absolute_entry:
                    sap_client.add_line_to_existing_attachment(
                        absolute_entry=absolute_entry,
                        file_path=attachment.file.path,
                        filename=attachment.original_filename,
                    )
                    abs_entry = absolute_entry
                else:
                    result = sap_client.upload_attachment(
                        file_path=attachment.file.path,
                        filename=attachment.original_filename,
                    )
                    abs_entry = result.get("AbsoluteEntry")
                if not abs_entry:
                    raise SAPDataError("SAP did not return attachment AbsoluteEntry.")
                absolute_entry = abs_entry
                attachment.sap_absolute_entry = abs_entry
                attachment.sap_attachment_status = "UPLOADED"
                attachment.sap_error_message = None
                attachment.save(
                    update_fields=[
                        "sap_absolute_entry",
                        "sap_attachment_status",
                        "sap_error_message",
                    ]
                )
            except (SAPValidationError, SAPConnectionError, SAPDataError) as exc:
                attachment.sap_attachment_status = "FAILED"
                attachment.sap_error_message = str(exc)
                attachment.save(
                    update_fields=["sap_attachment_status", "sap_error_message"]
                )
                raise
        return absolute_entry

    def _mark_posted(self, posting, user, doc_entry, doc_num, doc_total) -> None:
        posting.sap_doc_entry = doc_entry
        posting.sap_doc_num = doc_num
        posting.sap_doc_total = self._decimal(doc_total) if doc_total is not None else None
        posting.status = ARInvoiceStatus.POSTED
        posting.posted_at = timezone.now()
        posting.posted_by = user
        posting.updated_by = user
        posting.error_message = None
        posting.save(
            update_fields=[
                "sap_doc_entry", "sap_doc_num", "sap_doc_total", "status",
                "posted_at", "posted_by", "updated_by", "error_message",
                "updated_at",
            ]
        )

    def _mark_failed(self, posting, error_message: str, user) -> None:
        posting.status = ARInvoiceStatus.FAILED
        posting.error_message = error_message
        posting.updated_by = user
        posting.save(
            update_fields=["status", "error_message", "updated_by", "updated_at"]
        )

    @staticmethod
    def _decimal(value) -> Decimal:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
