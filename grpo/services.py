import logging
import os
import tempfile
from typing import List, Dict, Any, Optional
from decimal import Decimal
from django.utils import timezone
from django.db import transaction

from gate_core.enums import GateEntryStatus
from driver_management.models import VehicleEntry
from raw_material_gatein.models import POReceipt, POItemReceipt
from quality_control.enums import InspectionStatus
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .models import GRPOPosting, GRPOLinePosting, GRPOStatus, GRPOAttachment, SAPAttachmentStatus

logger = logging.getLogger(__name__)


class GRPOService:
    """
    Service for handling GRPO operations.
    """

    def __init__(self, company_code: str):
        self.company_code = company_code

    def get_pending_grpo_entries(self) -> List[VehicleEntry]:
        """
        Get all completed gate entries that are ready for GRPO posting.
        Returns entries with status COMPLETED or QC_COMPLETED.
        """
        return VehicleEntry.objects.filter(
            company__code=self.company_code,
            entry_type="RAW_MATERIAL",
            status__in=[GateEntryStatus.COMPLETED, GateEntryStatus.QC_COMPLETED]
        ).prefetch_related(
            "po_receipts",
            "po_receipts__items",
            "grpo_postings"
        ).order_by("-entry_time")

    def get_grpo_preview_data(
        self,
        vehicle_entry_id: int,
        po_receipt_ids: Optional[List[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all data required for GRPO posting for a specific gate entry.
        Optionally filter by specific PO receipt IDs (for merged preview).
        Returns list of PO receipts with their items and QC status.
        """
        try:
            vehicle_entry = VehicleEntry.objects.prefetch_related(
                "po_receipts",
                "po_receipts__items",
                "po_receipts__items__arrival_slip",
                "po_receipts__items__arrival_slip__inspection",
                "grpo_postings"
            ).get(id=vehicle_entry_id)
        except VehicleEntry.DoesNotExist:
            raise ValueError(f"Vehicle entry {vehicle_entry_id} not found")

        is_ready = vehicle_entry.status in [
            GateEntryStatus.COMPLETED,
            GateEntryStatus.QC_COMPLETED
        ]

        po_receipts_qs = vehicle_entry.po_receipts.all()
        if po_receipt_ids:
            po_receipts_qs = po_receipts_qs.filter(id__in=po_receipt_ids)

        result = []
        for po_receipt in po_receipts_qs:
            # Check if GRPO already posted for this PO (M2M or legacy FK)
            existing_grpo = GRPOPosting.objects.filter(
                po_receipts=po_receipt,
                status=GRPOStatus.POSTED
            ).first()
            if not existing_grpo:
                existing_grpo = vehicle_entry.grpo_postings.filter(
                    po_receipt=po_receipt,
                    status=GRPOStatus.POSTED
                ).first()

            items_data = []
            for item in po_receipt.items.all():
                qc_status = self._get_item_qc_status(item)
                items_data.append({
                    "po_item_receipt_id": item.id,
                    "item_code": item.po_item_code,
                    "item_name": item.item_name,
                    "ordered_qty": item.ordered_qty,
                    "received_qty": item.received_qty,
                    "accepted_qty": item.accepted_qty,
                    "rejected_qty": item.rejected_qty,
                    "uom": item.uom,
                    "qc_status": qc_status,
                    "unit_price": item.unit_price,
                    "tax_code": item.tax_code or "",
                    "warehouse_code": item.warehouse_code or "",
                    "gl_account": item.gl_account or "",
                    "variety": item.variety or "",
                    "sap_line_num": item.sap_line_num,
                })

            result.append({
                "vehicle_entry_id": vehicle_entry.id,
                "entry_no": vehicle_entry.entry_no,
                "entry_status": vehicle_entry.status,
                "entry_date": vehicle_entry.entry_time.date() if vehicle_entry.entry_time else None,
                "is_ready_for_grpo": is_ready,
                "po_receipt_id": po_receipt.id,
                "po_number": po_receipt.po_number,
                "supplier_code": po_receipt.supplier_code,
                "supplier_name": po_receipt.supplier_name,
                "sap_doc_entry": po_receipt.sap_doc_entry,
                "branch_id": po_receipt.branch_id,
                "vendor_ref": po_receipt.vendor_ref or "",
                "invoice_no": po_receipt.invoice_no or "",
                "invoice_date": po_receipt.invoice_date,
                "challan_no": po_receipt.challan_no or "",
                "items": items_data,
                "grpo_status": existing_grpo.status if existing_grpo else None,
                "sap_doc_num": existing_grpo.sap_doc_num if existing_grpo else None,
                "total_amount": existing_grpo.sap_doc_total if existing_grpo else None
            })

        return result

    def _get_item_qc_status(self, po_item_receipt: POItemReceipt) -> str:
        """Get QC status for a PO item receipt."""
        if not hasattr(po_item_receipt, "arrival_slip"):
            return "NO_ARRIVAL_SLIP"

        arrival_slip = po_item_receipt.arrival_slip
        if not arrival_slip.is_submitted:
            return "ARRIVAL_SLIP_PENDING"

        if not hasattr(arrival_slip, "inspection"):
            return "INSPECTION_PENDING"

        inspection = arrival_slip.inspection
        return inspection.final_status

    def _build_structured_comments(
        self,
        user,
        po_receipts: List[POReceipt],
        vehicle_entry: VehicleEntry,
        user_comments: Optional[str] = None
    ) -> str:
        """Build structured comments string for SAP GRPO."""
        full_name = user.get_full_name() if hasattr(user, 'get_full_name') else str(user)
        username = getattr(user, 'username', getattr(user, 'email', str(user)))

        po_numbers = ", ".join(po.po_number for po in po_receipts)
        parts = [
            f"App: FactoryApp v2",
            f"User: {full_name} ({username})",
            f"PO: {po_numbers}",
            f"Gate Entry: {vehicle_entry.entry_no}",
        ]

        if len(po_receipts) > 1:
            parts.append(f"Merged: {len(po_receipts)} POs")

        if user_comments:
            parts.append(user_comments)

        return " | ".join(parts)

    @transaction.atomic
    def post_grpo(
        self,
        vehicle_entry_id: int,
        po_receipt_ids: List[int],
        user,
        items: List[Dict[str, Any]],
        branch_id: int,
        warehouse_code: Optional[str] = None,
        comments: Optional[str] = None,
        vendor_ref: Optional[str] = None,
        extra_charges: Optional[List[Dict[str, Any]]] = None,
        attachments: Optional[list] = None,
        doc_date: Optional[str] = None,
        doc_due_date: Optional[str] = None,
        tax_date: Optional[str] = None,
        should_roundoff: bool = False,
    ) -> GRPOPosting:
        """
        Post GRPO to SAP for one or more PO receipts (merged GRPO).
        All PO receipts must belong to the same supplier and vehicle entry.

        Args:
            vehicle_entry_id: ID of the vehicle entry
            po_receipt_ids: List of PO receipt IDs to merge into single GRPO
            user: User posting the GRPO
            items: List of dicts with po_item_receipt_id, accepted_qty, and optional fields
            branch_id: SAP Branch/Business Place ID (BPLId)
            warehouse_code: Optional warehouse code for SAP
            comments: Optional user comments for SAP document
            vendor_ref: Optional vendor reference number (NumAtCard)
            extra_charges: Optional list of additional expense dicts
            attachments: Optional list of Django UploadedFile objects to attach
            doc_date: Optional posting date (DocDate), ISO format YYYY-MM-DD
            doc_due_date: Optional due date (DocDueDate), ISO format YYYY-MM-DD
            tax_date: Optional document date (TaxDate), ISO format YYYY-MM-DD
            should_roundoff: If True, auto-calculates RoundDif to round the subtotal to the nearest integer
        """
        # Get vehicle entry
        try:
            vehicle_entry = VehicleEntry.objects.get(id=vehicle_entry_id)
        except VehicleEntry.DoesNotExist:
            raise ValueError(f"Vehicle entry {vehicle_entry_id} not found")

        # Get all PO receipts
        po_receipts = list(
            POReceipt.objects.prefetch_related(
                "items",
                "items__arrival_slip",
                "items__arrival_slip__inspection"
            ).filter(id__in=po_receipt_ids, vehicle_entry=vehicle_entry)
        )

        if len(po_receipts) != len(po_receipt_ids):
            found_ids = {po.id for po in po_receipts}
            missing_ids = set(po_receipt_ids) - found_ids
            raise ValueError(f"PO receipt(s) not found for this vehicle entry: {missing_ids}")

        # Validate all POs have the same supplier
        supplier_codes = set(po.supplier_code for po in po_receipts)
        if len(supplier_codes) > 1:
            raise ValueError(
                f"Cannot merge POs from different suppliers. "
                f"Found suppliers: {supplier_codes}"
            )

        # Validate all POs have the same branch_id
        branch_ids = set(po.branch_id for po in po_receipts if po.branch_id is not None)
        if len(branch_ids) > 1:
            raise ValueError(
                f"Cannot merge POs with different branch IDs. "
                f"Found branch IDs: {branch_ids}"
            )

        # Validate gate entry status
        if vehicle_entry.status not in [
            GateEntryStatus.COMPLETED,
            GateEntryStatus.QC_COMPLETED
        ]:
            raise ValueError(
                f"Gate entry is not completed. Current status: {vehicle_entry.status}"
            )

        # Check if any PO already has a POSTED GRPO
        for po_receipt in po_receipts:
            existing = GRPOPosting.objects.filter(
                po_receipts=po_receipt,
                status=GRPOStatus.POSTED
            ).first()
            if not existing:
                # Also check legacy po_receipt FK
                existing = GRPOPosting.objects.filter(
                    vehicle_entry=vehicle_entry,
                    po_receipt=po_receipt,
                    status=GRPOStatus.POSTED
                ).first()
            if existing:
                raise ValueError(
                    f"GRPO already posted for PO {po_receipt.po_number}. "
                    f"SAP Doc Num: {existing.sap_doc_num}"
                )

        # Collect all item IDs across all PO receipts
        all_po_item_ids = set()
        for po_receipt in po_receipts:
            all_po_item_ids.update(po_receipt.items.values_list("id", flat=True))

        # Create a mapping of item IDs to input data
        items_input_map = {item["po_item_receipt_id"]: item for item in items}

        # Validate all item IDs belong to one of the selected PO receipts
        invalid_ids = set(items_input_map.keys()) - all_po_item_ids
        if invalid_ids:
            raise ValueError(f"Invalid PO item receipt IDs: {invalid_ids}")

        # Update accepted and rejected quantities in POItemReceipt
        for po_receipt in po_receipts:
            for item in po_receipt.items.all():
                if item.id in items_input_map:
                    accepted_qty = items_input_map[item.id]["accepted_qty"]
                    item.accepted_qty = accepted_qty
                    item.rejected_qty = max(item.received_qty - accepted_qty, Decimal("0"))
                    item.save()

        # Create GRPO posting record (use first PO as legacy po_receipt)
        grpo_posting = GRPOPosting.objects.create(
            vehicle_entry=vehicle_entry,
            po_receipt=po_receipts[0],
            status=GRPOStatus.PENDING,
            posted_by=user
        )
        # Link all PO receipts via M2M
        grpo_posting.po_receipts.set(po_receipts)

        # Build GRPO document lines from ALL PO receipts
        document_lines = []
        grpo_lines_data = []

        for po_receipt in po_receipts:
            for item in po_receipt.items.all():
                if item.accepted_qty <= 0:
                    continue

                item_input = items_input_map.get(item.id, {})

                line_data = {
                    "ItemCode": item.po_item_code,
                    "Quantity": str(item.accepted_qty),
                }

                # PO Linking — each line references its own PO's BaseEntry
                if po_receipt.sap_doc_entry and item.sap_line_num is not None:
                    line_data["BaseEntry"] = po_receipt.sap_doc_entry
                    line_data["BaseLine"] = item.sap_line_num
                    line_data["BaseType"] = 22  # Purchase Order

                if warehouse_code:
                    line_data["WarehouseCode"] = warehouse_code

                unit_price = item_input.get("unit_price")
                if unit_price is not None:
                    line_data["UnitPrice"] = float(unit_price)

                tax_code = item_input.get("tax_code")
                if tax_code:
                    line_data["TaxCode"] = tax_code

                gl_account = item_input.get("gl_account")
                if gl_account:
                    line_data["AccountCode"] = gl_account

                variety = item_input.get("variety")
                if variety:
                    line_data["CostingCode"] = variety

                document_lines.append(line_data)
                grpo_lines_data.append({
                    "po_item_receipt": item,
                    "quantity_posted": item.accepted_qty,
                    "base_entry": po_receipt.sap_doc_entry,
                    "base_line": item.sap_line_num,
                })

        if not document_lines:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = "No accepted quantities to post"
            grpo_posting.save()
            raise ValueError("No accepted quantities to post")

        # Build structured comments
        structured_comments = self._build_structured_comments(
            user, po_receipts, vehicle_entry, comments
        )

        # Build full SAP payload — CardCode from any PO (all same supplier)
        grpo_payload = {
            "CardCode": po_receipts[0].supplier_code,
            "BPL_IDAssignedToInvoice": branch_id,
            "Comments": structured_comments,
            "DocumentLines": document_lines
        }

        # Optional date fields
        if doc_date:
            grpo_payload["DocDate"] = str(doc_date)
        if doc_due_date:
            grpo_payload["DocDueDate"] = str(doc_due_date)
        if tax_date:
            grpo_payload["TaxDate"] = str(tax_date)

        # Auto round-off
        if should_roundoff:
            subtotal = Decimal('0')
            for line in document_lines:
                qty = Decimal(str(line.get("Quantity", 0)))
                price = Decimal(str(line.get("UnitPrice", 0)))
                subtotal += qty * price
            if subtotal > 0:
                rounded = subtotal.quantize(Decimal('1'), rounding='ROUND_HALF_UP')
                round_dif = float(rounded - subtotal)
                if round_dif != 0:
                    grpo_payload["RoundDif"] = round_dif

        if vendor_ref:
            grpo_payload["NumAtCard"] = vendor_ref

        # Extra charges (DocumentAdditionalExpenses)
        if extra_charges:
            additional_expenses = []
            for charge in extra_charges:
                expense = {
                    "ExpenseCode": charge["expense_code"],
                    "LineTotal": float(charge["amount"]),
                }
                if charge.get("remarks"):
                    expense["Remarks"] = charge["remarks"]
                if charge.get("tax_code"):
                    expense["TaxCode"] = charge["tax_code"]
                additional_expenses.append(expense)
            grpo_payload["DocumentAdditionalExpenses"] = additional_expenses

        # Upload attachments to SAP BEFORE creating GRPO
        sap_client = SAPClient(company_code=self.company_code)
        attachment_records = []
        sap_absolute_entry = None

        if attachments:
            for uploaded_file in attachments:
                suffix = os.path.splitext(uploaded_file.name)[1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    for chunk in uploaded_file.chunks():
                        tmp.write(chunk)
                    tmp_path = tmp.name

                try:
                    sap_result = sap_client.upload_attachment(
                        file_path=tmp_path,
                        filename=uploaded_file.name
                    )
                    abs_entry = sap_result.get("AbsoluteEntry")
                    if abs_entry:
                        sap_absolute_entry = abs_entry
                        attachment_records.append({
                            "file": uploaded_file,
                            "filename": uploaded_file.name,
                            "sap_absolute_entry": abs_entry,
                        })
                        logger.info(
                            f"Attachment '{uploaded_file.name}' uploaded to SAP. "
                            f"AbsoluteEntry: {abs_entry}"
                        )
                finally:
                    os.unlink(tmp_path)

            if sap_absolute_entry:
                grpo_payload["AttachmentEntry"] = sap_absolute_entry

        po_numbers_str = ", ".join(po.po_number for po in po_receipts)
        logger.info(f"GRPO Payload for PO(s) {po_numbers_str}: {grpo_payload}")

        # Post to SAP
        try:
            result = sap_client.create_grpo(grpo_payload)

            grpo_posting.sap_doc_entry = result.get("DocEntry")
            grpo_posting.sap_doc_num = result.get("DocNum")
            grpo_posting.sap_doc_total = Decimal(str(result.get("DocTotal", 0)))
            grpo_posting.status = GRPOStatus.POSTED
            grpo_posting.posted_at = timezone.now()
            grpo_posting.posted_by = user
            grpo_posting.save()

            for line_data in grpo_lines_data:
                GRPOLinePosting.objects.create(
                    grpo_posting=grpo_posting,
                    po_item_receipt=line_data["po_item_receipt"],
                    quantity_posted=line_data["quantity_posted"],
                    base_entry=line_data["base_entry"],
                    base_line=line_data["base_line"],
                )

            for att_data in attachment_records:
                GRPOAttachment.objects.create(
                    grpo_posting=grpo_posting,
                    file=att_data["file"],
                    original_filename=att_data["filename"],
                    sap_attachment_status=SAPAttachmentStatus.LINKED,
                    sap_absolute_entry=att_data["sap_absolute_entry"],
                    uploaded_by=user,
                )

            logger.info(
                f"GRPO posted successfully for PO(s) {po_numbers_str}. "
                f"SAP DocNum: {grpo_posting.sap_doc_num}"
            )

            return grpo_posting

        except SAPValidationError as e:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = str(e)
            grpo_posting.save()
            logger.error(f"SAP validation error posting GRPO: {e}")
            raise

        except SAPConnectionError as e:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = "SAP system unavailable"
            grpo_posting.save()
            logger.error(f"SAP connection error posting GRPO: {e}")
            raise

        except SAPDataError as e:
            grpo_posting.status = GRPOStatus.FAILED
            grpo_posting.error_message = str(e)
            grpo_posting.save()
            logger.error(f"SAP data error posting GRPO: {e}")
            raise

    def get_grpo_posting_history(
        self,
        vehicle_entry_id: Optional[int] = None
    ) -> List[GRPOPosting]:
        """Get GRPO posting history."""
        queryset = GRPOPosting.objects.select_related(
            "vehicle_entry",
            "po_receipt",
            "posted_by"
        ).prefetch_related("lines", "attachments", "po_receipts")

        if vehicle_entry_id:
            queryset = queryset.filter(vehicle_entry_id=vehicle_entry_id)

        return queryset.order_by("-created_at")

    def upload_grpo_attachment(
        self,
        grpo_posting_id: int,
        file,
        user
    ) -> GRPOAttachment:
        """
        Upload an attachment for a GRPO posting.
        1. Save file locally (via Django FileField)
        2. Upload to SAP Attachments2 endpoint
        3. Link to the GRPO document via PATCH
        4. Update local record with SAP response
        """
        # Validate GRPO posting exists and is POSTED
        try:
            grpo_posting = GRPOPosting.objects.get(id=grpo_posting_id)
        except GRPOPosting.DoesNotExist:
            raise ValueError(f"GRPO posting {grpo_posting_id} not found")

        if grpo_posting.status != GRPOStatus.POSTED:
            raise ValueError(
                f"Cannot attach files to GRPO with status '{grpo_posting.status}'. "
                f"Only POSTED GRPOs accept attachments."
            )

        if not grpo_posting.sap_doc_entry:
            raise ValueError("GRPO posting has no SAP DocEntry. Cannot upload attachment.")

        # Step 1: Save file locally
        attachment = GRPOAttachment.objects.create(
            grpo_posting=grpo_posting,
            file=file,
            original_filename=file.name,
            sap_attachment_status=SAPAttachmentStatus.PENDING,
            uploaded_by=user,
        )

        # Step 2: Upload to SAP
        try:
            sap_client = SAPClient(company_code=self.company_code)

            # Check if the GRPO already has an AttachmentEntry
            existing_abs_entry = sap_client.get_grpo_attachment_entry(
                grpo_posting.sap_doc_entry
            )

            if existing_abs_entry:
                # Add a new line to the existing Attachments2 entry.
                # This avoids PATCHing the GRPO document which triggers
                # SAP approval error (200039).
                sap_result = sap_client.add_line_to_existing_attachment(
                    absolute_entry=existing_abs_entry,
                    file_path=attachment.file.path,
                    filename=attachment.original_filename,
                )
                attachment.sap_absolute_entry = existing_abs_entry
                attachment.sap_attachment_status = SAPAttachmentStatus.LINKED
                attachment.save(update_fields=[
                    "sap_absolute_entry", "sap_attachment_status"
                ])
            else:
                # No existing attachment — upload and include in GRPO
                sap_result = sap_client.upload_attachment(
                    file_path=attachment.file.path,
                    filename=attachment.original_filename
                )
                absolute_entry = sap_result.get("AbsoluteEntry")
                if not absolute_entry:
                    raise SAPDataError("SAP did not return AbsoluteEntry")

                attachment.sap_absolute_entry = absolute_entry
                attachment.sap_attachment_status = SAPAttachmentStatus.UPLOADED
                attachment.save(update_fields=[
                    "sap_absolute_entry", "sap_attachment_status"
                ])

                # Link attachment to the GRPO document
                sap_client.link_attachment_to_grpo(
                    doc_entry=grpo_posting.sap_doc_entry,
                    absolute_entry=absolute_entry
                )
                attachment.sap_attachment_status = SAPAttachmentStatus.LINKED
                attachment.save(update_fields=["sap_attachment_status"])

            logger.info(
                f"Attachment '{attachment.original_filename}' uploaded and linked "
                f"to GRPO DocEntry {grpo_posting.sap_doc_entry}"
            )

            return attachment

        except (SAPValidationError, SAPConnectionError, SAPDataError) as e:
            attachment.sap_attachment_status = SAPAttachmentStatus.FAILED
            attachment.sap_error_message = str(e)
            attachment.save(update_fields=[
                "sap_attachment_status", "sap_error_message"
            ])
            logger.error(
                f"Failed to upload attachment for GRPO {grpo_posting_id}: {e}"
            )
            # Return attachment with FAILED status — file is saved locally
            return attachment

    def retry_attachment_upload(
        self,
        attachment_id: int,
    ) -> GRPOAttachment:
        """
        Retry uploading a FAILED attachment to SAP.
        If upload succeeded but link failed, skips re-upload.
        """
        try:
            attachment = GRPOAttachment.objects.select_related(
                "grpo_posting"
            ).get(id=attachment_id)
        except GRPOAttachment.DoesNotExist:
            raise ValueError(f"Attachment {attachment_id} not found")

        if attachment.sap_attachment_status not in [
            SAPAttachmentStatus.PENDING,
            SAPAttachmentStatus.FAILED
        ]:
            raise ValueError(
                f"Attachment is already '{attachment.sap_attachment_status}'. "
                f"Only PENDING or FAILED attachments can be retried."
            )

        grpo_posting = attachment.grpo_posting
        if not grpo_posting.sap_doc_entry:
            raise ValueError("GRPO posting has no SAP DocEntry.")

        try:
            sap_client = SAPClient(company_code=self.company_code)

            # Check if GRPO already has an AttachmentEntry
            existing_abs_entry = sap_client.get_grpo_attachment_entry(
                grpo_posting.sap_doc_entry
            )

            if existing_abs_entry:
                # Add line to existing Attachments2 entry (avoids approval error)
                sap_client.add_line_to_existing_attachment(
                    absolute_entry=existing_abs_entry,
                    file_path=attachment.file.path,
                    filename=attachment.original_filename,
                )
                attachment.sap_absolute_entry = existing_abs_entry
                attachment.sap_attachment_status = SAPAttachmentStatus.LINKED
                attachment.sap_error_message = None
                attachment.save(update_fields=[
                    "sap_absolute_entry", "sap_attachment_status",
                    "sap_error_message"
                ])
            else:
                # No existing attachment — upload and link
                if attachment.sap_absolute_entry:
                    absolute_entry = attachment.sap_absolute_entry
                else:
                    sap_result = sap_client.upload_attachment(
                        file_path=attachment.file.path,
                        filename=attachment.original_filename
                    )
                    absolute_entry = sap_result.get("AbsoluteEntry")
                    if not absolute_entry:
                        raise SAPDataError("SAP did not return AbsoluteEntry")

                    attachment.sap_absolute_entry = absolute_entry
                    attachment.sap_attachment_status = SAPAttachmentStatus.UPLOADED
                    attachment.save(update_fields=[
                        "sap_absolute_entry", "sap_attachment_status"
                    ])

                sap_client.link_attachment_to_grpo(
                    doc_entry=grpo_posting.sap_doc_entry,
                    absolute_entry=absolute_entry
                )
                attachment.sap_attachment_status = SAPAttachmentStatus.LINKED
                attachment.sap_error_message = None
                attachment.save(update_fields=[
                    "sap_attachment_status", "sap_error_message"
                ])

            return attachment

        except (SAPValidationError, SAPConnectionError, SAPDataError) as e:
            attachment.sap_attachment_status = SAPAttachmentStatus.FAILED
            attachment.sap_error_message = str(e)
            attachment.save(update_fields=[
                "sap_attachment_status", "sap_error_message"
            ])
            return attachment
