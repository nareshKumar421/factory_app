import logging
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

    def get_grpo_preview_data(self, vehicle_entry_id: int) -> List[Dict[str, Any]]:
        """
        Get all data required for GRPO posting for a specific gate entry.
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

        result = []
        for po_receipt in vehicle_entry.po_receipts.all():
            # Check if GRPO already posted for this PO
            existing_grpo = vehicle_entry.grpo_postings.filter(
                po_receipt=po_receipt
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
                    # Pre-filled from PO for GRPO posting
                    "unit_price": item.unit_price,
                    "tax_code": item.tax_code or "",
                    "warehouse_code": item.warehouse_code or "",
                    "gl_account": item.gl_account or "",
                    "sap_line_num": item.sap_line_num,
                })

            result.append({
                "vehicle_entry_id": vehicle_entry.id,
                "entry_no": vehicle_entry.entry_no,
                "entry_status": vehicle_entry.status,
                "is_ready_for_grpo": is_ready,
                "po_receipt_id": po_receipt.id,
                "po_number": po_receipt.po_number,
                "supplier_code": po_receipt.supplier_code,
                "supplier_name": po_receipt.supplier_name,
                "sap_doc_entry": po_receipt.sap_doc_entry,
                # Pre-filled from PO for GRPO posting
                "branch_id": po_receipt.branch_id,
                "vendor_ref": po_receipt.vendor_ref or "",
                "invoice_no": po_receipt.invoice_no or "",
                "invoice_date": po_receipt.invoice_date,
                "challan_no": po_receipt.challan_no or "",
                "items": items_data,
                "grpo_status": existing_grpo.status if existing_grpo else None,
                "sap_doc_num": existing_grpo.sap_doc_num if existing_grpo else None
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
        po_receipt: POReceipt,
        vehicle_entry: VehicleEntry,
        user_comments: Optional[str] = None
    ) -> str:
        """Build structured comments string for SAP GRPO."""
        full_name = user.get_full_name() if hasattr(user, 'get_full_name') else str(user)
        username = getattr(user, 'username', getattr(user, 'email', str(user)))

        parts = [
            f"App: FactoryApp v2",
            f"User: {full_name} ({username})",
            f"PO: {po_receipt.po_number}",
            f"Gate Entry: {vehicle_entry.entry_no}",
        ]

        if user_comments:
            parts.append(user_comments)

        return " | ".join(parts)

    @transaction.atomic
    def post_grpo(
        self,
        vehicle_entry_id: int,
        po_receipt_id: int,
        user,
        items: List[Dict[str, Any]],
        branch_id: int,
        warehouse_code: Optional[str] = None,
        comments: Optional[str] = None,
        vendor_ref: Optional[str] = None,
        extra_charges: Optional[List[Dict[str, Any]]] = None,
        attachments: Optional[list] = None,
    ) -> GRPOPosting:
        """
        Post GRPO to SAP for a specific PO receipt.
        Updates accepted quantities in POItemReceipt before posting.

        Args:
            vehicle_entry_id: ID of the vehicle entry
            po_receipt_id: ID of the PO receipt
            user: User posting the GRPO
            items: List of dicts with po_item_receipt_id, accepted_qty, and optional fields
            branch_id: SAP Branch/Business Place ID (BPLId)
            warehouse_code: Optional warehouse code for SAP
            comments: Optional user comments for SAP document
            vendor_ref: Optional vendor reference number (NumAtCard)
            extra_charges: Optional list of additional expense dicts
        """
        # Get vehicle entry and PO receipt
        try:
            vehicle_entry = VehicleEntry.objects.get(id=vehicle_entry_id)
            po_receipt = POReceipt.objects.prefetch_related(
                "items",
                "items__arrival_slip",
                "items__arrival_slip__inspection"
            ).get(id=po_receipt_id, vehicle_entry=vehicle_entry)
        except VehicleEntry.DoesNotExist:
            raise ValueError(f"Vehicle entry {vehicle_entry_id} not found")
        except POReceipt.DoesNotExist:
            raise ValueError(f"PO receipt {po_receipt_id} not found for this vehicle entry")

        # Create a mapping of item IDs to input data
        items_input_map = {item["po_item_receipt_id"]: item for item in items}

        # Validate all item IDs belong to this PO receipt
        po_item_ids = set(po_receipt.items.values_list("id", flat=True))
        invalid_ids = set(items_input_map.keys()) - po_item_ids
        if invalid_ids:
            raise ValueError(f"Invalid PO item receipt IDs: {invalid_ids}")

        # Update accepted and rejected quantities in POItemReceipt
        for item in po_receipt.items.all():
            if item.id in items_input_map:
                accepted_qty = items_input_map[item.id]["accepted_qty"]
                # Validate accepted_qty doesn't exceed received_qty
                if accepted_qty > item.received_qty:
                    raise ValueError(
                        f"Accepted qty ({accepted_qty}) cannot exceed received qty "
                        f"({item.received_qty}) for item {item.item_name}"
                    )
                item.accepted_qty = accepted_qty
                item.rejected_qty = item.received_qty - accepted_qty
                item.save()

        # Check if already posted
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

        # Validate gate entry status
        if vehicle_entry.status not in [
            GateEntryStatus.COMPLETED,
            GateEntryStatus.QC_COMPLETED
        ]:
            raise ValueError(
                f"Gate entry is not completed. Current status: {vehicle_entry.status}"
            )

        # Create or get GRPO posting record
        grpo_posting, created = GRPOPosting.objects.get_or_create(
            vehicle_entry=vehicle_entry,
            po_receipt=po_receipt,
            defaults={
                "status": GRPOStatus.PENDING,
                "posted_by": user
            }
        )

        # Build GRPO document lines
        document_lines = []
        grpo_lines_data = []

        for item in po_receipt.items.all():
            # Only post accepted quantities
            if item.accepted_qty <= 0:
                continue

            # Get per-item input data (unit_price, tax_code, etc.)
            item_input = items_input_map.get(item.id, {})

            line_data = {
                "ItemCode": item.po_item_code,
                "Quantity": str(item.accepted_qty),
            }

            # PO Linking - BaseEntry/BaseLine/BaseType
            if po_receipt.sap_doc_entry and item.sap_line_num is not None:
                line_data["BaseEntry"] = po_receipt.sap_doc_entry
                line_data["BaseLine"] = item.sap_line_num
                line_data["BaseType"] = 22  # 22 = Purchase Order

            if warehouse_code:
                line_data["WarehouseCode"] = warehouse_code

            # Optional line-level fields from request
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
                line_data["U_Variety"] = variety
                line_data["U_SchemeAgst"] = variety

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
            raise ValueError("No accepted quantities to post for this PO")

        # Build structured comments
        structured_comments = self._build_structured_comments(
            user, po_receipt, vehicle_entry, comments
        )

        # Build full SAP payload
        grpo_payload = {
            "CardCode": po_receipt.supplier_code,
            "BPL_IDAssignedToInvoice": branch_id,
            "Comments": structured_comments,
            "DocumentLines": document_lines
        }

        # Optional header fields
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

        # Log payload for debugging
        logger.info(f"GRPO Payload for PO {po_receipt.po_number}: {grpo_payload}")

        # Post to SAP
        try:
            sap_client = SAPClient(company_code=self.company_code)

            # Upload attachments to SAP BEFORE creating GRPO
            # This avoids the approval re-trigger error (200039) caused by
            # PATCHing AttachmentEntry on an already-posted document.
            attachment_records = []
            sap_absolute_entry = None

            if attachments:
                for file in attachments:
                    # Save file locally first
                    attachment = GRPOAttachment.objects.create(
                        grpo_posting=grpo_posting,
                        file=file,
                        original_filename=file.name,
                        sap_attachment_status=SAPAttachmentStatus.PENDING,
                        uploaded_by=user,
                    )
                    attachment_records.append(attachment)

                    try:
                        sap_result = sap_client.upload_attachment(
                            file_path=attachment.file.path,
                            filename=attachment.original_filename,
                        )
                        absolute_entry = sap_result.get("AbsoluteEntry")
                        if not absolute_entry:
                            raise SAPDataError("SAP did not return AbsoluteEntry")

                        attachment.sap_absolute_entry = absolute_entry
                        attachment.sap_attachment_status = SAPAttachmentStatus.UPLOADED
                        attachment.save(update_fields=[
                            "sap_absolute_entry", "sap_attachment_status"
                        ])

                        # Use the last uploaded attachment's AbsoluteEntry
                        # SAP links one AttachmentEntry per document
                        sap_absolute_entry = absolute_entry

                        logger.info(
                            f"Attachment '{file.name}' uploaded to SAP. "
                            f"AbsoluteEntry: {absolute_entry}"
                        )
                    except (SAPValidationError, SAPConnectionError, SAPDataError) as e:
                        attachment.sap_attachment_status = SAPAttachmentStatus.FAILED
                        attachment.sap_error_message = str(e)
                        attachment.save(update_fields=[
                            "sap_attachment_status", "sap_error_message"
                        ])
                        logger.warning(
                            f"Attachment '{file.name}' upload failed: {e}. "
                            f"GRPO will be posted without this attachment."
                        )

            # Include AttachmentEntry in the GRPO payload if upload succeeded
            if sap_absolute_entry:
                grpo_payload["AttachmentEntry"] = sap_absolute_entry

            result = sap_client.create_grpo(grpo_payload)

            # Update GRPO posting with SAP response
            grpo_posting.sap_doc_entry = result.get("DocEntry")
            grpo_posting.sap_doc_num = result.get("DocNum")
            grpo_posting.sap_doc_total = Decimal(str(result.get("DocTotal", 0)))
            grpo_posting.status = GRPOStatus.POSTED
            grpo_posting.posted_at = timezone.now()
            grpo_posting.posted_by = user
            grpo_posting.save()

            # Mark uploaded attachments as LINKED (they are part of the document now)
            for attachment in attachment_records:
                if attachment.sap_attachment_status == SAPAttachmentStatus.UPLOADED:
                    attachment.sap_attachment_status = SAPAttachmentStatus.LINKED
                    attachment.save(update_fields=["sap_attachment_status"])

            # Create line posting records with PO linking info
            for line_data in grpo_lines_data:
                GRPOLinePosting.objects.create(
                    grpo_posting=grpo_posting,
                    po_item_receipt=line_data["po_item_receipt"],
                    quantity_posted=line_data["quantity_posted"],
                    base_entry=line_data["base_entry"],
                    base_line=line_data["base_line"],
                )

            logger.info(
                f"GRPO posted successfully for PO {po_receipt.po_number}. "
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
        ).prefetch_related("lines", "attachments")

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

        # Step 2: Upload to SAP Attachments2
        try:
            sap_client = SAPClient(company_code=self.company_code)
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

            # Step 3: Link attachment to the GRPO document
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

            # If upload succeeded but link failed, skip re-upload
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

            # Link to document
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
