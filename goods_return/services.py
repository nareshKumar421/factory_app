"""Company-scoped orchestration for the Goods Return module.

Business errors raise ``ValueError`` (views translate to HTTP 400); cross-company
access violations raise DRF ``PermissionDenied`` (403). Invoice details are read on
demand from SAP via ``DispatchPlansService`` -- never stored beyond the doc-entry
reference and a per-line snapshot needed for display / over-return validation.
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from vehicle_management.models import Vehicle

from .models import (
    GoodsReturn,
    GoodsReturnAttachment,
    GoodsReturnBasis,
    GoodsReturnInvoiceRef,
    GoodsReturnItem,
    GoodsReturnStatus,
)

logger = logging.getLogger(__name__)

EDITABLE_STATUSES = (GoodsReturnStatus.DRAFT,)


def _generate_vehicle_entry_no() -> str:
    today = timezone.now()
    prefix = f"GRV-{today.strftime('%Y%m%d')}"
    last = (
        VehicleEntry.objects.filter(entry_no__startswith=prefix)
        .order_by("-entry_no")
        .first()
    )
    seq = 1
    if last:
        try:
            seq = int(last.entry_no.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    return f"{prefix}-{seq:04d}"


class GoodsReturnService:
    """Orchestration bound to the active-header company for create/invoice lookup.

    Edit/read methods resolve the company from the *record* and enforce that it is
    one of the caller's companies (``allowed_company_ids``) -- the cross-company
    security boundary.
    """

    def __init__(self, company: Company | None = None):
        self.company = company

    # -- helpers ---------------------------------------------------------------

    def _dispatch_service(self, company: Company):
        # Imported lazily: dispatch_plans pulls in the SAP/HANA reader stack.
        from dispatch_plans.services import DispatchPlansService

        return DispatchPlansService(company.code)

    def _lookup_bill(self, company: Company, invoice_number: str) -> dict:
        try:
            bill = self._dispatch_service(company).get_bill_by_number(invoice_number)
        except Exception as exc:  # SAP/HANA read failures surface as a clean 400
            logger.warning("Goods return invoice lookup failed for %s: %s", invoice_number, exc)
            raise ValueError(f"Could not look up invoice {invoice_number}.")
        if not bill:
            raise ValueError(f"No SAP invoice found for {invoice_number}.")
        return bill

    def _get_scoped(self, pk, allowed_company_ids) -> GoodsReturn:
        gr = (
            GoodsReturn.objects.filter(pk=pk, is_active=True)
            .select_related("company", "vehicle", "driver", "vehicle_entry")
            .prefetch_related("invoice_refs", "lines", "attachments")
            .first()
        )
        if gr is None:
            raise ValueError("Goods return not found.")
        if gr.company_id not in allowed_company_ids:
            raise PermissionDenied("This record belongs to a company you cannot access.")
        return gr

    def _assert_editable(self, gr: GoodsReturn):
        if gr.status not in EDITABLE_STATUSES:
            raise ValueError(f"A {gr.get_status_display()} return can no longer be edited.")

    # -- reads -----------------------------------------------------------------

    def list_returns(self, company_ids, *, status=None, basis=None, search=None):
        qs = (
            GoodsReturn.objects.filter(is_active=True, company_id__in=company_ids)
            .select_related("company", "vehicle", "driver")
            .prefetch_related("lines")
        )
        if status:
            qs = qs.filter(status=status)
        if basis:
            qs = qs.filter(basis=basis)
        if search:
            qs = qs.filter(entry_no__icontains=search) | qs.filter(
                customer_name__icontains=search
            )
        return qs

    def get_return(self, pk, allowed_company_ids) -> GoodsReturn:
        return self._get_scoped(pk, allowed_company_ids)

    def get_invoice_preview(self, gr: GoodsReturn) -> list[dict]:
        """Live invoice header/lines for the return's referenced bills (on demand)."""
        previews = []
        for ref in gr.active_invoice_refs:
            try:
                bill = self._dispatch_service(gr.company).get_bill_by_number(
                    ref.sap_invoice_doc_num or str(ref.sap_invoice_doc_entry)
                )
            except Exception:
                bill = None
            if bill:
                previews.append(
                    {
                        "invoice_ref_id": ref.id,
                        "doc_entry": bill.get("doc_entry"),
                        "doc_num": bill.get("doc_num"),
                        "card_code": bill.get("card_code"),
                        "card_name": bill.get("card_name"),
                        "items": bill.get("items", []),
                    }
                )
        return previews

    # -- create ----------------------------------------------------------------

    @transaction.atomic
    def create_return(self, data, user) -> GoodsReturn:
        if self.company is None:
            raise ValueError("A company context is required to create a return.")

        basis = data["basis"]
        gr = GoodsReturn(
            company=self.company,
            entry_no=GoodsReturn.generate_entry_no(),
            basis=basis,
            status=GoodsReturnStatus.DRAFT,
            customer_code=(data.get("customer_code") or "").strip(),
            customer_name=(data.get("customer_name") or "").strip(),
            remarks=(data.get("remarks") or "").strip(),
            created_by=user,
        )

        if basis == GoodsReturnBasis.INVOICE:
            invoice_numbers = data.get("invoice_numbers") or []
            if not invoice_numbers:
                raise ValueError("Select at least one invoice for an invoice-based return.")
            gr.save()
            for number in invoice_numbers:
                self._attach_invoice(gr, str(number).strip())
        else:
            if not gr.customer_name:
                raise ValueError("Enter the customer name.")
            gr.save()

        return gr

    def _attach_invoice(self, gr: GoodsReturn, invoice_number: str) -> GoodsReturnInvoiceRef:
        bill = self._lookup_bill(gr.company, invoice_number)
        doc_entry = bill["doc_entry"]

        if gr.invoice_refs.filter(sap_invoice_doc_entry=doc_entry, is_active=True).exists():
            raise ValueError(f"Invoice {bill.get('doc_num') or invoice_number} is already added.")

        card_code = (bill.get("card_code") or "").strip()
        card_name = (bill.get("card_name") or "").strip()
        # A return is for one customer; block mixing bills across customers.
        if gr.customer_code and card_code and card_code != gr.customer_code:
            raise ValueError("All invoices on a return must be for the same customer.")
        if not gr.customer_code:
            gr.customer_code = card_code
            gr.customer_name = card_name
            gr.save(update_fields=["customer_code", "customer_name", "updated_at"])

        ref = GoodsReturnInvoiceRef.objects.create(
            goods_return=gr,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=bill.get("doc_num") or "",
        )
        # Snapshot invoice lines as candidate return items (return_quantity=0 until
        # the operator fills Step 2).
        for line in bill.get("items", []):
            GoodsReturnItem.objects.create(
                goods_return=gr,
                invoice_ref=ref,
                source_line_num=line.get("line_num"),
                item_code=line.get("item_code") or "",
                item_name=line.get("item_name") or "",
                uom=line.get("uom") or "",
                invoice_quantity=line.get("quantity") or 0,
                return_quantity=0,
            )
        return ref

    # -- header / invoice-ref edits -------------------------------------------

    def update_header(self, pk, data, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)
        for field in ("customer_code", "customer_name", "remarks"):
            if field in data:
                setattr(gr, field, (data.get(field) or "").strip())
        gr.updated_by = user
        gr.save()
        return gr

    def add_invoice_ref(self, pk, invoice_number, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)
        if gr.basis != GoodsReturnBasis.INVOICE:
            raise ValueError("Invoices can only be added to an invoice-based return.")
        with transaction.atomic():
            self._attach_invoice(gr, str(invoice_number).strip())
            gr.updated_by = user
            gr.save(update_fields=["updated_by", "updated_at"])
        return gr

    def remove_invoice_ref(self, pk, ref_id, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)
        ref = gr.invoice_refs.filter(pk=ref_id, is_active=True).first()
        if ref is None:
            raise ValueError("Invoice reference not found.")
        with transaction.atomic():
            # Hard-remove the ref and its snapshotted lines (draft only).
            gr.lines.filter(invoice_ref=ref).delete()
            ref.delete()
            gr.updated_by = user
            gr.save(update_fields=["updated_by", "updated_at"])
        return gr

    # -- items (Step 2) --------------------------------------------------------

    @transaction.atomic
    def save_items(self, pk, lines, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)

        ref_by_id = {ref.id: ref for ref in gr.active_invoice_refs}
        cleaned = []
        for raw in lines:
            qty = raw.get("return_quantity") or 0
            if qty <= 0:
                continue  # a line not being returned
            invoice_qty = raw.get("invoice_quantity") or 0
            if invoice_qty and qty > invoice_qty:
                raise ValueError(
                    f"Return quantity ({qty}) cannot exceed invoice quantity ({invoice_qty}) "
                    f"for {raw.get('item_code') or 'an item'}."
                )
            ref_id = raw.get("invoice_ref_id")
            if ref_id is not None and ref_id not in ref_by_id:
                raise ValueError("Unknown invoice reference on a return line.")
            cleaned.append((raw, ref_id))

        if not cleaned:
            raise ValueError("Add at least one item with a return quantity.")

        gr.lines.all().delete()
        for raw, ref_id in cleaned:
            GoodsReturnItem.objects.create(
                goods_return=gr,
                invoice_ref=ref_by_id.get(ref_id) if ref_id else None,
                source_line_num=raw.get("source_line_num"),
                item_code=(raw.get("item_code") or "").strip(),
                item_name=(raw.get("item_name") or "").strip(),
                uom=(raw.get("uom") or "").strip(),
                invoice_quantity=raw.get("invoice_quantity") or 0,
                return_quantity=raw.get("return_quantity"),
                reason=(raw.get("reason") or "").strip(),
                condition=raw.get("condition") or "DAMAGED",
                remarks=(raw.get("remarks") or "").strip(),
            )
        gr.updated_by = user
        gr.save(update_fields=["updated_by", "updated_at"])
        return gr

    # -- vehicle (Step 3) ------------------------------------------------------

    def set_vehicle(self, pk, data, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        self._assert_editable(gr)

        vehicle = Vehicle.objects.filter(pk=data["vehicle_id"]).first()
        if vehicle is None:
            raise ValueError("Selected vehicle not found.")
        driver = Driver.objects.filter(pk=data["driver_id"]).first()
        if driver is None:
            raise ValueError("Selected driver not found.")

        gr.vehicle = vehicle
        gr.driver = driver
        gr.expected_arrival_at = data["expected_arrival_at"]
        gr.updated_by = user
        gr.save(
            update_fields=[
                "vehicle",
                "driver",
                "expected_arrival_at",
                "updated_by",
                "updated_at",
            ]
        )
        return gr

    # -- submit ----------------------------------------------------------------

    @transaction.atomic
    def submit(self, pk, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        if gr.status != GoodsReturnStatus.DRAFT:
            raise ValueError("Only a draft return can be submitted.")
        if not gr.active_lines:
            raise ValueError("Add at least one returning item before submitting.")
        if not gr.vehicle_id or not gr.driver_id:
            raise ValueError("Select the return vehicle and driver before submitting.")
        if not gr.expected_arrival_at:
            raise ValueError("Set the expected gate arrival before submitting.")
        if not gr.attachments.exists():
            raise ValueError("Attach at least one supporting document before submitting.")

        # SAP goods-return / return-document posting hook (stubbed for now).
        self._post_to_sap_stub(gr)

        gr.status = GoodsReturnStatus.AWAITING_ARRIVAL
        gr.submitted_by = user
        gr.submitted_at = timezone.now()
        gr.updated_by = user
        gr.save(
            update_fields=[
                "status",
                "submitted_by",
                "submitted_at",
                "updated_by",
                "updated_at",
            ]
        )
        return gr

    def _post_to_sap_stub(self, gr: GoodsReturn):
        # TODO(goods-return): post a SAP Returns / GRPO document here and store
        # sap_gr_doc_entry / sap_gr_doc_num. Deferred -- no SAP return writer yet.
        logger.info("SAP goods-return posting is stubbed; %s submitted without a SAP doc.", gr.entry_no)
        return None

    def cancel(self, pk, user, allowed_company_ids) -> GoodsReturn:
        gr = self._get_scoped(pk, allowed_company_ids)
        if gr.status == GoodsReturnStatus.ARRIVED:
            raise ValueError("A return already marked in at the gate cannot be cancelled.")
        gr.status = GoodsReturnStatus.CANCELLED
        gr.updated_by = user
        gr.save(update_fields=["status", "updated_by", "updated_at"])
        return gr

    # -- attachments -----------------------------------------------------------

    def list_attachments(self, pk, allowed_company_ids):
        gr = self._get_scoped(pk, allowed_company_ids)
        return gr.attachments.all()

    def upload_attachment(self, pk, file, attachment_type, notes, user, allowed_company_ids):
        gr = self._get_scoped(pk, allowed_company_ids)
        return GoodsReturnAttachment.objects.create(
            goods_return=gr,
            attachment_type=attachment_type or "OTHER",
            file=file,
            original_filename=getattr(file, "name", "")[:255],
            notes=(notes or "").strip(),
            uploaded_by=user,
        )

    def delete_attachment(self, pk, attachment_id, allowed_company_ids):
        gr = self._get_scoped(pk, allowed_company_ids)
        attachment = gr.attachments.filter(pk=attachment_id).first()
        if attachment is None:
            raise ValueError("Attachment not found.")
        attachment.delete()


# ===========================================================================
# Gate side -- cross-company (expected list + mark-in)
# ===========================================================================

def list_expected_returns(company_ids):
    """Returns awaiting a gate arrival for the caller's companies."""
    return (
        GoodsReturn.objects.filter(
            status=GoodsReturnStatus.AWAITING_ARRIVAL,
            vehicle_entry__isnull=True,
            is_active=True,
            company_id__in=company_ids,
        )
        .select_related("company", "vehicle", "driver")
        .order_by("expected_arrival_at", "id")
    )


@transaction.atomic
def mark_return_in(pk, user, data, company_ids) -> GoodsReturn:
    """Gate marks the return's vehicle in: create a shared VehicleEntry (the truck
    reads as inside) and flip the return to ARRIVED. Company resolved from the record."""
    gr = (
        GoodsReturn.objects.select_for_update(of=("self",))
        .filter(pk=pk, is_active=True)
        .select_related("company", "vehicle", "driver")
        .first()
    )
    if gr is None:
        raise ValueError("Goods return not found.")
    if gr.company_id not in company_ids:
        raise PermissionDenied("This record belongs to a company you cannot access.")
    if gr.status != GoodsReturnStatus.AWAITING_ARRIVAL or gr.vehicle_entry_id:
        raise ValueError("This return is not awaiting a gate arrival.")
    if not gr.vehicle_id or not gr.driver_id:
        raise ValueError("This return has no vehicle/driver to mark in.")

    vehicle_entry = VehicleEntry.objects.create(
        entry_no=_generate_vehicle_entry_no(),
        company=gr.company,
        vehicle=gr.vehicle,
        driver=gr.driver,
        entry_type="GOODS_RETURN",
        status=GateEntryStatus.IN_PROGRESS,  # truck is now inside
        created_by=user,
        remarks=(data.get("remarks") or "").strip(),
    )
    gr.vehicle_entry = vehicle_entry
    gr.status = GoodsReturnStatus.ARRIVED
    gr.gated_in_by = user
    gr.gated_in_at = timezone.now()
    gr.updated_by = user
    gr.save(
        update_fields=[
            "vehicle_entry",
            "status",
            "gated_in_by",
            "gated_in_at",
            "updated_by",
            "updated_at",
        ]
    )
    return gr
