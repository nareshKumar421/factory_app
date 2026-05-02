# quality_control/models/raw_material_inspection.py

from django.db import models
from django.conf import settings
from django.utils import timezone
from gate_core.models import BaseModel
from ..enums import (
    ArrivalSlipStatus,
    FactoryHeadDecision,
    InspectionStatus,
    InspectionWorkflowStatus,
)

User = settings.AUTH_USER_MODEL


class RawMaterialInspection(BaseModel):
    """
    Raw Material Inspection Report - filled by QA/Lab personnel.
    Linked to MaterialArrivalSlip (each arrival slip gets its own inspection).
    """
    arrival_slip = models.OneToOneField(
        "quality_control.MaterialArrivalSlip",
        on_delete=models.CASCADE,
        related_name="inspection",
        null=True,  # Temporarily nullable for migration
        blank=True
    )

    # Auto-generated identifiers
    report_no = models.CharField(max_length=50, unique=True)
    internal_lot_no = models.CharField(max_length=50, unique=False)  # Not unique across all inspections, but unique per day

    # Inspection Date
    inspection_date = models.DateField()

    # Material Information (can be pre-filled from arrival slip/PO item)
    description_of_material = models.TextField()
    sap_code = models.CharField(max_length=50, blank=True)

    # Supplier Information
    supplier_name = models.CharField(max_length=200)
    manufacturer_name = models.CharField(max_length=200, blank=True)
    supplier_batch_lot_no = models.CharField(max_length=100, blank=True)

    # Unit and PO Information
    unit_packing = models.CharField(max_length=100, blank=True)
    purchase_order_no = models.CharField(max_length=50, blank=True)

    # Internal Report Number (manually filled by QC)
    internal_report_no = models.CharField(max_length=100, blank=True)
    invoice_bill_no = models.CharField(max_length=100, blank=True)

    # Vehicle number for reference
    vehicle_no = models.CharField(max_length=50, blank=True)

    # Material Type (for dynamic parameters)
    material_type = models.ForeignKey(
        "quality_control.MaterialType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections"
    )

    # Final Status
    final_status = models.CharField(
        max_length=20,
        choices=InspectionStatus.choices,
        default=InspectionStatus.PENDING
    )

    # Approval Chain - QA Chemist
    qa_chemist = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections_as_chemist"
    )
    qa_chemist_approved_at = models.DateTimeField(null=True, blank=True)
    qa_chemist_remarks = models.TextField(blank=True)

    # Approval Chain - QA Manager
    qam = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections_as_qam"
    )
    qam_approved_at = models.DateTimeField(null=True, blank=True)
    qam_remarks = models.TextField(blank=True)

    # Rejection tracking
    rejected_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections_rejected"
    )
    rejected_at = models.DateTimeField(null=True, blank=True)

    # Factory Head decision after QA Manager rejection
    factory_head = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections_factory_head_decided"
    )
    factory_head_decision = models.CharField(
        max_length=30,
        choices=FactoryHeadDecision.choices,
        blank=True,
        default=""
    )
    factory_head_remarks = models.TextField(blank=True)
    factory_head_decided_at = models.DateTimeField(null=True, blank=True)

    # Workflow status
    workflow_status = models.CharField(
        max_length=30,
        choices=InspectionWorkflowStatus.choices,
        default=InspectionWorkflowStatus.DRAFT
    )

    is_locked = models.BooleanField(default=False)
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workflow_status"]),
            models.Index(fields=["final_status"]),
            models.Index(fields=["factory_head_decision"]),
            models.Index(fields=["inspection_date"]),
        ]
        permissions = [
            ("can_submit_inspection", "Can submit inspection for approval"),
            ("can_approve_as_chemist", "Can approve inspection as QA Chemist"),
            ("can_approve_as_qam", "Can approve inspection as QA Manager"),
            ("can_reject_inspection", "Can reject inspection"),
            ("can_factory_head_decision", "Can record factory head QC decision"),
        ]

    def __str__(self):
        return f"Inspection {self.report_no} - {self.description_of_material[:50]}"

    @property
    def po_item_receipt(self):
        """Convenience property to access POItemReceipt via arrival slip."""
        return self.arrival_slip.po_item_receipt

    @property
    def vehicle_entry(self):
        """Convenience property to access VehicleEntry via chain."""
        return self.arrival_slip.po_item_receipt.po_receipt.vehicle_entry

    @property
    def effective_final_status(self):
        """Current usable QC status after any factory head override."""
        if self.factory_head_decision == FactoryHeadDecision.ACCEPT_QC_OVERRIDE:
            return InspectionStatus.ACCEPTED
        return self.final_status

    @property
    def is_rejected_qc_returned(self):
        """Whether this rejected QC item has already been sent out at gate."""
        return hasattr(self, "rejected_qc_return_item")

    @property
    def rejected_qc_return_entry(self):
        """Return gate-out entry, if this rejected QC item has been sent out."""
        try:
            return self.rejected_qc_return_item.entry
        except Exception:
            return None

    @staticmethod
    def generate_report_no():
        """Generate unique report number."""
        from django.utils import timezone
        today = timezone.now()
        prefix = f"RPT-{today.strftime('%Y%m%d')}"
        last = RawMaterialInspection.objects.filter(
            report_no__startswith=prefix
        ).order_by("-report_no").first()

        if last:
            try:
                last_num = int(last.report_no.split("-")[-1])
                new_num = last_num + 1
            except ValueError:
                new_num = 1
        else:
            new_num = 1

        return f"{prefix}-{new_num:04d}"

    @staticmethod
    def generate_lot_no():
        """Generate unique internal lot number."""
        from django.utils import timezone
        today = timezone.now()
        prefix = f"LOT-{today.strftime('%Y%m%d')}"
        last = RawMaterialInspection.objects.filter(
            internal_lot_no__startswith=prefix
        ).order_by("-internal_lot_no").first()

        if last:
            try:
                last_num = int(last.internal_lot_no.split("-")[-1])
                new_num = last_num + 1
            except ValueError:
                new_num = 1
        else:
            new_num = 1

        return f"{prefix}-{new_num:04d}"

    def submit_for_approval(self):
        """Submit inspection for QA Chemist review."""
        self.workflow_status = InspectionWorkflowStatus.SUBMITTED
        self.save(update_fields=["workflow_status", "updated_at"])

    def approve_by_chemist(self, user, remarks=""):
        """QA Chemist approval."""
        self.qa_chemist = user
        self.qa_chemist_approved_at = timezone.now()
        self.qa_chemist_remarks = remarks
        self.workflow_status = InspectionWorkflowStatus.QA_CHEMIST_APPROVED
        self.save(update_fields=[
            "qa_chemist", "qa_chemist_approved_at",
            "qa_chemist_remarks", "workflow_status", "updated_at"
        ])

    def approve_by_qam(self, user, remarks="", final_status=InspectionStatus.ACCEPTED):
        """QA Manager final approval."""
        self.qam = user
        self.qam_approved_at = timezone.now()
        self.qam_remarks = remarks
        self.workflow_status = InspectionWorkflowStatus.QAM_APPROVED
        self.final_status = final_status
        self.is_locked = True
        self.save(update_fields=[
            "qam", "qam_approved_at", "qam_remarks",
            "workflow_status", "final_status", "is_locked", "updated_at"
        ])

    def cancel_for_send_back(self, user, remarks=""):
        """Soft-delete this draft inspection when the arrival slip is sent back to gate."""
        self.is_active = False
        self.is_locked = True
        self.arrival_slip = None  # Free the OneToOne so a new inspection can be created
        self.remarks = remarks or "Cancelled — arrival slip sent back to gate for correction"
        self.updated_by = user
        self.save(update_fields=[
            "is_active", "is_locked", "arrival_slip",
            "remarks", "updated_by", "updated_at",
        ])
        # Also soft-delete the parameter results
        self.parameter_results.update(is_active=False)

    def reject(self, user, remarks=""):
        """Reject inspection - sends back to security guard."""
        self.final_status = InspectionStatus.REJECTED
        self.workflow_status = InspectionWorkflowStatus.REJECTED
        self.is_locked = True
        self.remarks = remarks
        self.rejected_by = user
        self.rejected_at = timezone.now()
        self.updated_by = user
        self.save(update_fields=[
            "final_status", "workflow_status", "is_locked", "remarks",
            "rejected_by", "rejected_at", "updated_by", "updated_at"
        ])
        # Also mark arrival slip as rejected
        self.arrival_slip.reject_by_qa(remarks=remarks)

    def record_factory_head_decision(self, user, decision, remarks=""):
        """Record factory head decision after QA rejection.

        Only the accept override changes the operational QC outcome. Other
        decisions are stored for workflow visibility until their downstream
        actions are implemented.
        """
        self.factory_head = user
        self.factory_head_decision = decision
        self.factory_head_remarks = remarks
        self.factory_head_decided_at = timezone.now()
        self.updated_by = user

        update_fields = [
            "factory_head", "factory_head_decision", "factory_head_remarks",
            "factory_head_decided_at", "updated_by", "updated_at",
        ]

        if decision == FactoryHeadDecision.ACCEPT_QC_OVERRIDE:
            self.final_status = InspectionStatus.ACCEPTED
            self.workflow_status = InspectionWorkflowStatus.QAM_APPROVED
            self.is_locked = True
            update_fields.extend(["final_status", "workflow_status", "is_locked"])

            if self.arrival_slip:
                self.arrival_slip.status = ArrivalSlipStatus.SUBMITTED
                self.arrival_slip.is_submitted = True
                self.arrival_slip.save(update_fields=["status", "is_submitted", "updated_at"])

        self.save(update_fields=update_fields)
