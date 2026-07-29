"""Returnable Gate Pass — items that leave the gate temporarily and must come back.

Header (:class:`ReturnableGatePass`) → lines (:class:`ReturnableGatePassItem`) →
zero or more return trips (:class:`ReturnableReturnEvent` → its lines).

A line's ``quantity_returned`` is the running sum of every return-event line that
points at it, so partial and multi-trip returns fall out naturally.
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone

from gate_core.models.base import BaseModel

from .constants import (
    AttachmentDocType,
    ItemConditionOut,
    ItemReturnCondition,
    ReturnableLogAction,
    ReturnablePurpose,
    ReturnableStatus,
)

ZERO = Decimal("0.000")


class ReturnablePermission(models.Model):
    """Sentinel model carrying the module's custom permissions.

    Mirrors ``maintenance.MaintenancePermission`` — unmanaged, no table.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "Returnable Permission"
        permissions = [
            ("can_view_returnable_module", "Can view returnable items module"),
            ("can_view_returnable_gatepass", "Can view returnable gate passes"),
            ("can_manage_returnable_gatepass", "Can create and edit returnable gate passes"),
            ("can_submit_returnable_gatepass", "Can submit a returnable gate pass for approval"),
            ("can_approve_returnable_gatepass", "Can approve or reject a returnable gate pass"),
            ("can_gate_out_returnable", "Can gate out a returnable gate pass"),
            ("can_gate_in_returnable", "Can record a returnable gate pass return at the gate"),
            ("can_reject_returnable_at_gate", "Can reject a returnable gate pass at the gate"),
            ("can_acknowledge_returnable", "Can acknowledge receipt of returned items"),
            ("can_close_returnable", "Can close a returnable gate pass"),
            ("can_short_close_returnable", "Can short close a returnable gate pass"),
            ("can_cancel_returnable", "Can cancel a returnable gate pass"),
            ("can_view_returnable_reports", "Can view returnable item reports"),
        ]


class ReturnableGatePassSequence(models.Model):
    """Per-company, per-financial-year running number for gate passes.

    Uses ``select_for_update`` rather than the scan-for-last-row numbering used
    elsewhere in ``maintenance`` — gate operators create passes in bursts and
    that pattern collides.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="returnable_gatepass_sequences",
    )
    financial_year = models.CharField(max_length=9)
    #: ``RGP`` for returnable, ``NRGP`` for non-returnable. Each gets its own counter.
    prefix = models.CharField(max_length=8, default="RGP")
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("company", "financial_year", "prefix")
        verbose_name = "Returnable Gate Pass Sequence"
        verbose_name_plural = "Returnable Gate Pass Sequences"

    def __str__(self):
        return f"{self.prefix} {self.company_id} {self.financial_year}: {self.last_number}"

    @staticmethod
    def current_financial_year():
        today = timezone.localdate()
        start_year = today.year if today.month >= 4 else today.year - 1
        return f"{start_year}-{str(start_year + 1)[-2:]}"

    @classmethod
    def next_pass_no(cls, company, is_returnable=True):
        prefix = "RGP" if is_returnable else "NRGP"
        financial_year = cls.current_financial_year()
        with transaction.atomic():
            sequence, _ = cls.objects.select_for_update().get_or_create(
                company=company,
                financial_year=financial_year,
                prefix=prefix,
                defaults={"last_number": 0},
            )
            sequence.last_number += 1
            sequence.save(update_fields=["last_number", "updated_at"])
            return f"{prefix}/{financial_year}/{sequence.last_number:06d}"


class ReturnableGatePass(BaseModel):
    """One document covering a set of items leaving the gate temporarily."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="returnable_gatepasses",
    )
    # Unique per company, not globally: the number sequence is per-company, so
    # two companies legitimately share "RGP/2026-27/000001". A global unique
    # constraint would make the second company's first pass collide.
    pass_no = models.CharField(max_length=30)
    status = models.CharField(
        max_length=20,
        choices=ReturnableStatus.choices,
        default=ReturnableStatus.DRAFT,
    )
    #: False for material that leaves and never comes back — issued to a person,
    #: scrap sales, samples. A non-returnable pass closes the moment it is gated
    #: out: no return trips, no acknowledgement, no overdue clock.
    is_returnable = models.BooleanField(default=True)

    # --- raised by -------------------------------------------------------
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_gatepasses",
    )
    requested_by_name = models.CharField(max_length=200, blank=True, default="")
    contact_no = models.CharField(max_length=50, blank=True, default="")

    # --- why -------------------------------------------------------------
    purpose = models.CharField(
        max_length=20,
        choices=ReturnablePurpose.choices,
        default=ReturnablePurpose.REPAIR,
    )
    purpose_detail = models.TextField(blank=True, default="")

    # --- where to --------------------------------------------------------
    #: The vendor a returnable pass goes to. Optional on a non-returnable pass,
    #: where the destination is a person rather than a firm.
    party_name = models.CharField(max_length=200, blank=True, default="")
    party_contact = models.CharField(max_length=50, blank=True, default="")
    party_address = models.TextField(blank=True, default="")
    party_gstin = models.CharField(max_length=20, blank=True, default="")

    # --- who receives it (non-returnable) --------------------------------
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_received",
    )
    recipient_name = models.CharField(max_length=200, blank=True, default="")
    recipient_contact = models.CharField(max_length=50, blank=True, default="")
    recipient_department = models.CharField(max_length=150, blank=True, default="")
    #: Who handed the material over. The counterpart to ``recipient_name`` on a
    #: non-returnable pass, where ``requested_by_name`` makes no sense — nobody
    #: is requesting anything back.
    issued_by_name = models.CharField(max_length=200, blank=True, default="")

    # --- when back (returnable only) -------------------------------------
    expected_return_date = models.DateField(null=True, blank=True)
    is_overdue = models.BooleanField(default=False)
    due_notified_at = models.DateTimeField(null=True, blank=True)
    overdue_notified_at = models.DateTimeField(null=True, blank=True)

    # --- optional links --------------------------------------------------
    asset = models.ForeignKey(
        "maintenance.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_gatepasses",
    )
    work_order = models.ForeignKey(
        "maintenance.MaintenanceWorkOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_gatepasses",
    )

    # --- gate-out block (filled by the gate, not the department) ----------
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_gatepasses_out",
    )
    driver = models.ForeignKey(
        "driver_management.Driver",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_gatepasses_out",
    )
    transporter = models.ForeignKey(
        "vehicle_management.Transporter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_gatepasses_out",
    )
    vehicle_number_manual = models.CharField(max_length=30, blank=True, default="")
    driver_name_manual = models.CharField(max_length=200, blank=True, default="")
    driver_mobile = models.CharField(max_length=20, blank=True, default="")
    security_name = models.CharField(max_length=200, blank=True, default="")
    out_remarks = models.TextField(blank=True, default="")

    #: Some material simply walks out — a gauge carried to a workshop across the
    #: road. There is no vehicle to record, so the gate captures who carried it.
    is_hand_carried = models.BooleanField(default=False)
    carried_by_name = models.CharField(max_length=200, blank=True, default="")

    # --- lifecycle stamps ------------------------------------------------
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    # The higher authority who signs off before the gate ever sees the pass.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    gate_out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_gated_out",
    )
    gate_out_at = models.DateTimeField(null=True, blank=True)
    last_return_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_closed",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_cancelled",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default="")
    #: Why the approver sent it back. Distinct from ``rejected_reason``, which is
    #: the gate bouncing a physically mismatched pass.
    approval_rejected_reason = models.TextField(blank=True, default="")
    rejected_reason = models.TextField(blank=True, default="")
    short_close_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "pass_no"],
                name="uniq_returnable_pass_no_per_company",
            )
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "expected_return_date"]),
            models.Index(fields=["company", "is_overdue"]),
        ]
        verbose_name = "Returnable Gate Pass"
        verbose_name_plural = "Returnable Gate Passes"

    def __str__(self):
        return f"{self.pass_no} - {self.destination}"

    @property
    def destination(self):
        """Who the material went to — a vendor, or a person on a non-returnable pass."""
        if self.is_returnable:
            return self.party_name or "—"
        return self.recipient_name or self.party_name or "—"

    # --- derived ---------------------------------------------------------
    @property
    def total_estimated_value(self):
        return sum((item.estimated_value or ZERO for item in self.items.all()), ZERO)

    @property
    def total_quantity_out(self):
        return sum((item.quantity_out for item in self.items.all()), ZERO)

    @property
    def total_quantity_returned(self):
        return sum((item.quantity_returned for item in self.items.all()), ZERO)

    @property
    def pending_return_qty(self):
        return self.total_quantity_out - self.total_quantity_returned

    @property
    def is_fully_returned(self):
        items = list(self.items.all())
        return bool(items) and all(item.is_fully_returned for item in items)

    @property
    def days_overdue(self):
        """Whole days past the expected return date, 0 if not yet due or settled.

        A non-returnable pass has no return date and can never be overdue.
        """
        if not self.is_returnable or not self.expected_return_date:
            return 0
        if self.status not in (ReturnableStatus.OUT, ReturnableStatus.PARTIALLY_RETURNED):
            return 0
        delta = (timezone.localdate() - self.expected_return_date).days
        return max(delta, 0)

    def refresh_status(self):
        """Derive OUT / PARTIALLY_RETURNED / RETURNED from the line totals.

        Only meaningful once the pass has left the gate; earlier statuses and the
        terminal ones are left untouched.
        """
        if self.status not in (
            ReturnableStatus.OUT,
            ReturnableStatus.PARTIALLY_RETURNED,
            ReturnableStatus.RETURNED,
        ):
            return

        items = list(self.items.all())
        total_out = sum((item.quantity_out for item in items), ZERO)
        total_returned = sum((item.quantity_returned for item in items), ZERO)

        if total_returned <= ZERO:
            self.status = ReturnableStatus.OUT
        elif total_returned >= total_out:
            self.status = ReturnableStatus.RETURNED
        else:
            self.status = ReturnableStatus.PARTIALLY_RETURNED

        # Once everything is back, the pass can no longer be overdue.
        if self.status == ReturnableStatus.RETURNED:
            self.is_overdue = False

    def log(self, action, actor=None, note="", meta=None):
        return ReturnableGatePassLog.objects.create(
            gate_pass=self,
            action=action,
            actor=actor,
            note=note,
            meta=meta or {},
        )

    def save(self, *args, **kwargs):
        if not self.pass_no:
            self.pass_no = ReturnableGatePassSequence.next_pass_no(
                self.company, is_returnable=self.is_returnable
            )
        super().save(*args, **kwargs)


class ReturnableGatePassItem(BaseModel):
    """One line of material on a gate pass."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="returnable_gatepass_items",
    )
    gate_pass = models.ForeignKey(
        ReturnableGatePass,
        on_delete=models.CASCADE,
        related_name="items",
    )
    line_num = models.PositiveIntegerField(default=1)

    #: SAP ``OITM.ItemCode`` when the line was picked from the item master, blank
    #: when it was typed in free-hand (a returnable pass often carries an
    #: unlisted asset — a motor, a die, a gauge).
    item_code = models.CharField(max_length=100, blank=True, default="")
    item_name = models.CharField(max_length=250)
    description = models.TextField(blank=True, default="")
    serial_no = models.CharField(max_length=120, blank=True, default="")
    make_model = models.CharField(max_length=200, blank=True, default="")

    spare = models.ForeignKey(
        "maintenance.MaintenanceSpare",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_lines",
    )
    asset = models.ForeignKey(
        "maintenance.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_lines",
    )

    uom = models.CharField(max_length=20, blank=True, default="NOS")
    quantity_out = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    quantity_returned = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=ZERO,
    )
    condition_out = models.CharField(
        max_length=20,
        choices=ItemConditionOut.choices,
        default=ItemConditionOut.FAULTY,
    )
    estimated_value = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
    )
    remarks = models.CharField(max_length=250, blank=True, default="")

    class Meta:
        ordering = ["line_num", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["gate_pass", "line_num"],
                name="uniq_returnable_line_num_per_pass",
            )
        ]
        verbose_name = "Returnable Gate Pass Item"
        verbose_name_plural = "Returnable Gate Pass Items"

    def __str__(self):
        return f"{self.item_name} x {self.quantity_out}"

    @property
    def pending_return_qty(self):
        return self.quantity_out - self.quantity_returned

    @property
    def is_fully_returned(self):
        return self.quantity_returned >= self.quantity_out

    def recalculate_returned(self, save=True):
        """Re-sum ``quantity_returned`` from the return-event lines pointing here."""
        total = self.return_lines.aggregate(total=models.Sum("quantity_returned"))["total"]
        self.quantity_returned = total or ZERO
        if save:
            self.save(update_fields=["quantity_returned", "updated_at"])
        return self.quantity_returned


class ReturnableReturnEvent(BaseModel):
    """One physical return trip against a gate pass. A pass may have several."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="returnable_return_events",
    )
    gate_pass = models.ForeignKey(
        ReturnableGatePass,
        on_delete=models.CASCADE,
        related_name="return_events",
    )
    event_no = models.PositiveIntegerField(default=1)
    event_ref = models.CharField(max_length=40, blank=True, default="")
    returned_at = models.DateTimeField(default=timezone.now)

    # The returning vehicle need not be the vehicle that took the items out —
    # vendors routinely send their own. Captured independently.
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_return_events",
    )
    driver = models.ForeignKey(
        "driver_management.Driver",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_return_events",
    )
    transporter = models.ForeignKey(
        "vehicle_management.Transporter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_return_events",
    )
    vehicle_number_manual = models.CharField(max_length=30, blank=True, default="")
    driver_name_manual = models.CharField(max_length=200, blank=True, default="")
    driver_mobile = models.CharField(max_length=20, blank=True, default="")
    security_name = models.CharField(max_length=200, blank=True, default="")

    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_events_verified",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_events_acknowledged",
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["event_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["gate_pass", "event_no"],
                name="uniq_returnable_event_no_per_pass",
            )
        ]
        verbose_name = "Returnable Return Event"
        verbose_name_plural = "Returnable Return Events"

    def __str__(self):
        return self.event_ref or f"{self.gate_pass_id}-R{self.event_no}"

    @property
    def is_acknowledged(self):
        return self.acknowledged_at is not None

    @property
    def vehicle_number(self):
        if self.vehicle_id and self.vehicle:
            return self.vehicle.vehicle_number
        return self.vehicle_number_manual


class ReturnableReturnEventItem(BaseModel):
    """How much of one pass line came back on one return trip."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="returnable_return_event_items",
    )
    event = models.ForeignKey(
        ReturnableReturnEvent,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    pass_item = models.ForeignKey(
        ReturnableGatePassItem,
        on_delete=models.CASCADE,
        related_name="return_lines",
    )
    quantity_returned = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    return_condition = models.CharField(
        max_length=20,
        choices=ItemReturnCondition.choices,
        default=ItemReturnCondition.OK,
    )
    remarks = models.CharField(max_length=250, blank=True, default="")

    class Meta:
        ordering = ["id"]
        verbose_name = "Returnable Return Event Item"
        verbose_name_plural = "Returnable Return Event Items"

    def __str__(self):
        return f"{self.pass_item_id} +{self.quantity_returned}"


class ReturnableGatePassAttachment(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="returnable_attachments",
    )
    gate_pass = models.ForeignKey(
        ReturnableGatePass,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="returnable-items/attachments/")
    doc_type = models.CharField(
        max_length=20,
        choices=AttachmentDocType.choices,
        default=AttachmentDocType.OTHER,
    )
    caption = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Returnable Gate Pass Attachment"
        verbose_name_plural = "Returnable Gate Pass Attachments"

    def __str__(self):
        return self.caption or f"attachment {self.pk}"


class ReturnableGatePassLog(models.Model):
    """Append-only timeline. The backend has no simple-history, so we keep our own."""

    gate_pass = models.ForeignKey(
        ReturnableGatePass,
        on_delete=models.CASCADE,
        related_name="logs",
    )
    action = models.CharField(max_length=25, choices=ReturnableLogAction.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returnable_logs",
    )
    at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True, default="")
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at", "-id"]
        verbose_name = "Returnable Gate Pass Log"
        verbose_name_plural = "Returnable Gate Pass Logs"

    def __str__(self):
        return f"{self.gate_pass_id} {self.action}"
