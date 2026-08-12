# quality_control/models/raw_material_inspection.py

from django.db import models
from django.conf import settings
from django.utils import timezone
from gate_core.models import BaseModel
from ..enums import (
    InspectionDecision,
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

    # Manually entered by QC (no longer auto-generated)
    report_no = models.CharField(max_length=50, unique=True)
    internal_lot_no = models.CharField(max_length=50, unique=False)  # Not unique across all inspections

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

    # Which vendor's parameter set was applied. The vendor is taken from the
    # PO (POReceipt.supplier_code) rather than typed, so the spec used can't be
    # picked by hand; an override is possible but has to be justified.
    parameter_set = models.ForeignKey(
        "quality_control.QCParameterSet",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspections"
    )
    vendor_code = models.CharField(max_length=30, blank=True, default="")
    vendor_name = models.CharField(max_length=150, blank=True, default="")
    vendor_override_reason = models.TextField(
        blank=True,
        default="",
        help_text="Why the QC vendor differs from the one on the PO"
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
    qa_chemist_decision = models.CharField(
        max_length=20,
        choices=InspectionDecision.choices,
        blank=True,
        default=""
    )
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
    qam_decision = models.CharField(
        max_length=20,
        choices=InspectionDecision.choices,
        blank=True,
        default=""
    )
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
            models.Index(fields=["qa_chemist_decision"]),
            models.Index(fields=["qam_decision"]),
            models.Index(fields=["inspection_date"]),
        ]
        permissions = [
            ("can_submit_inspection", "Can submit inspection for approval"),
            ("can_approve_as_chemist", "Can approve inspection as QA Chemist"),
            ("can_approve_as_qam", "Can approve inspection as QA Manager"),
            ("can_reject_inspection", "Can reject inspection"),
            ("can_override_qc_vendor", "Can inspect against a vendor other than the PO's"),
        ]

    def __str__(self):
        return f"Inspection {self.report_no} - {self.description_of_material[:50]}"

    @property
    def po_vendor_code(self):
        """SAP supplier code from the PO this inspection came through."""
        slip = self.arrival_slip
        if not slip or not slip.po_item_receipt_id:
            return ""
        return slip.po_item_receipt.po_receipt.supplier_code or ""

    @property
    def is_vendor_overridden(self):
        """Whether QC inspected against a vendor other than the PO's supplier."""
        po_vendor = (self.po_vendor_code or "").strip().upper()
        if not po_vendor:
            return False
        return (self.vendor_code or "").strip().upper() != po_vendor

    @property
    def parameter_set_label(self):
        """Human label for the parameter set applied, for reports and lists."""
        if not self.parameter_set_id:
            return ""
        return self.parameter_set.label

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
        """Current usable QC status. The QA Manager's decision is final."""
        return self.final_status

    @property
    def is_grpo_done(self):
        """Whether a GRPO has been posted to SAP for this inspection's item.

        Once goods receipt is posted the QC outcome is committed downstream, so
        the QA Manager can no longer change the decision.
        """
        slip = self.arrival_slip
        if not slip:
            return False
        po_item = getattr(slip, "po_item_receipt", None)
        if not po_item:
            return False
        # Imported lazily to avoid a hard import cycle at app-load time.
        from grpo.models import GRPOStatus
        return po_item.grpo_lines.filter(
            grpo_posting__status=GRPOStatus.POSTED
        ).exists()

    @staticmethod
    def decision_to_final_status(decision):
        """Map simplified approval decision to stored manager final status."""
        if decision == InspectionDecision.APPROVED:
            return InspectionStatus.ACCEPTED
        if decision == InspectionDecision.REJECTED:
            return InspectionStatus.REJECTED
        if decision == InspectionDecision.HOLD:
            return InspectionStatus.HOLD
        return InspectionStatus.PENDING

    @staticmethod
    def final_status_to_decision(final_status):
        """Map stored final status to simplified approval decision."""
        if final_status == InspectionStatus.ACCEPTED:
            return InspectionDecision.APPROVED
        if final_status == InspectionStatus.REJECTED:
            return InspectionDecision.REJECTED
        if final_status == InspectionStatus.HOLD:
            return InspectionDecision.HOLD
        return ""

    @property
    def qc_stage(self):
        """Simplified workflow stage for API consumers."""
        if self.workflow_status == InspectionWorkflowStatus.DRAFT:
            return "DRAFT"
        if self.workflow_status == InspectionWorkflowStatus.SUBMITTED:
            return "AWAITING_CHEMIST"
        if self.workflow_status == InspectionWorkflowStatus.QA_CHEMIST_APPROVED:
            return "AWAITING_MANAGER"
        if self.workflow_status in [
            InspectionWorkflowStatus.QAM_APPROVED,
            InspectionWorkflowStatus.REJECTED,
            InspectionWorkflowStatus.COMPLETED,
        ]:
            return "DECIDED"
        return self.workflow_status

    @property
    def manager_decision(self):
        """Operational QC decision, owned by the QA Manager."""
        return self.qam_decision or self.final_status_to_decision(self.final_status)

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

    def submit_for_approval(self, user=None):
        """Submit inspection for QA Chemist review."""
        self.workflow_status = InspectionWorkflowStatus.SUBMITTED
        update_fields = ["workflow_status", "updated_at"]
        if user is not None:
            self.updated_by = user
            update_fields.append("updated_by")
        self.save(update_fields=update_fields)

    def approve_by_chemist(self, user, remarks="", decision=InspectionDecision.APPROVED):
        """Record QA Chemist decision and move to QA Manager review."""
        self.qa_chemist = user
        self.qa_chemist_approved_at = timezone.now()
        self.qa_chemist_decision = decision
        self.qa_chemist_remarks = remarks
        self.workflow_status = InspectionWorkflowStatus.QA_CHEMIST_APPROVED
        self.updated_by = user
        self.save(update_fields=[
            "qa_chemist", "qa_chemist_approved_at",
            "qa_chemist_decision", "qa_chemist_remarks",
            "workflow_status", "updated_at"
        ])

    def approve_by_qam(
        self,
        user,
        remarks="",
        final_status=InspectionStatus.ACCEPTED,
        decision=None,
    ):
        """Record QA Manager final QC decision."""
        manager_decision = decision or self.final_status_to_decision(final_status)
        manager_final_status = self.decision_to_final_status(manager_decision)
        self.qam = user
        self.qam_approved_at = timezone.now()
        self.qam_decision = manager_decision
        self.qam_remarks = remarks
        self.workflow_status = InspectionWorkflowStatus.QAM_APPROVED
        self.final_status = manager_final_status
        self.is_locked = True
        update_fields = [
            "qam", "qam_approved_at", "qam_decision", "qam_remarks",
            "workflow_status", "final_status", "is_locked", "updated_by", "updated_at"
        ]
        if manager_final_status == InspectionStatus.REJECTED:
            self.rejected_by = user
            self.rejected_at = timezone.now()
            self.remarks = remarks
            update_fields.extend(["rejected_by", "rejected_at", "remarks"])
        else:
            # A revised decision that is no longer a rejection must not leave
            # stale rejection tracking behind (e.g. REJECTED -> APPROVED).
            self.rejected_by = None
            self.rejected_at = None
            update_fields.extend(["rejected_by", "rejected_at"])
        self.updated_by = user
        self.save(update_fields=update_fields)

        # Record the decision in the audit trail (every call, so a changed
        # decision leaves a visible history rather than overwriting silently).
        self.manager_decision_logs.create(
            decision=manager_decision,
            remarks=remarks,
            decided_by=user,
            decided_at=self.qam_approved_at,
            created_by=user,
        )

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
        """Legacy reject action mapped to the current actor's decision."""
        if self.workflow_status == InspectionWorkflowStatus.SUBMITTED:
            self.approve_by_chemist(
                user=user,
                remarks=remarks,
                decision=InspectionDecision.REJECTED,
            )
            return

        self.approve_by_qam(
            user=user,
            remarks=remarks,
            decision=InspectionDecision.REJECTED,
        )


class InspectionManagerDecisionLog(BaseModel):
    """Audit trail of QA Manager decisions on a raw material inspection.

    A new row is written every time the manager records a decision, including
    when they overturn a previous one, so the full history of who decided what
    (and why) stays visible even though the inspection only stores the latest.
    """
    inspection = models.ForeignKey(
        "quality_control.RawMaterialInspection",
        on_delete=models.CASCADE,
        related_name="manager_decision_logs",
    )
    decision = models.CharField(
        max_length=20,
        choices=InspectionDecision.choices,
    )
    remarks = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_decision_logs",
    )
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-decided_at", "-id"]
        indexes = [
            models.Index(fields=["inspection", "-decided_at"]),
        ]

    def __str__(self):
        return f"Inspection {self.inspection_id} · {self.decision} by {self.decided_by_id}"
