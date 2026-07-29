from django.conf import settings
from django.db import models

from company.models import Company

from .constants import ItemFamily, WarehouseRoleType


class WarehouseRole(models.Model):
    """
    Per-company classification of a SAP warehouse (OWHS.WhsCode) within the
    production material-movement flow.

    This is the single source of truth the movement wrapper consults to decide,
    for a given company:
      - which warehouse GRPO for packaging is received into (`is_grpo_target`),
      - which warehouse BOM is issued from (`is_bom_issue_point`),
      - which stores feed the issue point (`feeds_whs_code`),
      - where finished goods land (role FG_RECEIPT).

    Warehouse codes intentionally differ per company (e.g. Oil issues from BH-PC,
    Beverages from BH-PP), which is exactly why this is data-driven rather than
    hardcoded. `warehouse_name` is a cached display copy of OWHS.WhsName; the
    authoritative stock still comes live from HANA.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="warehouse_roles"
    )
    whs_code = models.CharField(max_length=20, help_text="SAP OWHS.WhsCode")
    warehouse_name = models.CharField(max_length=200, blank=True, default="")

    role = models.CharField(max_length=30, choices=WarehouseRoleType.CHOICES)
    family = models.CharField(
        max_length=10, choices=ItemFamily.CHOICES, default=ItemFamily.PM
    )

    is_grpo_target = models.BooleanField(
        default=False,
        help_text="Packaging GRPO for this company is received into this warehouse.",
    )
    is_bom_issue_point = models.BooleanField(
        default=False,
        help_text="Production BOM is issued ONLY from this warehouse (one per company/family).",
    )
    feeds_whs_code = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="If a store, the issue-point warehouse it replenishes.",
    )
    transfer_needs_request = models.BooleanField(
        default=False,
        help_text=(
            "Moving FROM this store to the issue point requires a SAP Inventory "
            "Transfer Request first (SP rule 67081). E.g. Oil BH-PM->BH-PC = True, "
            "BH-BS->BH-PC = False."
        ),
    )

    is_active = models.BooleanField(default=True)
    needs_review = models.BooleanField(
        default=False,
        help_text="Provisional mapping pending user confirmation (e.g. Beverages).",
    )
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("company", "whs_code")
        ordering = ["company__code", "whs_code"]
        permissions = [
            ("can_view_warehouse_roles", "Can view warehouse role config"),
            ("can_manage_warehouse_roles", "Can manage warehouse role config"),
            ("can_view_production_stock", "Can view production warehouse stock"),
        ]

    def __str__(self):
        return f"{self.company.code}:{self.whs_code} [{self.role}]"


class MovementType:
    GRPO_RECEIPT = "GRPO_RECEIPT"
    TRANSFER_REQUEST = "TRANSFER_REQUEST"
    TRANSFER = "TRANSFER"
    BOM_ISSUE = "BOM_ISSUE"
    FG_RECEIPT = "FG_RECEIPT"

    CHOICES = [
        (GRPO_RECEIPT, "GRPO Receipt"),
        (TRANSFER_REQUEST, "Inventory Transfer Request"),
        (TRANSFER, "Stock Transfer"),
        (BOM_ISSUE, "BOM Issue"),
        (FG_RECEIPT, "FG Receipt"),
    ]


class MovementStatus:
    DRAFT = "DRAFT"
    DRY_RUN = "DRY_RUN"      # built + validated but not posted (SAP writes disabled)
    POSTED = "POSTED"
    FAILED = "FAILED"

    CHOICES = [
        (DRAFT, "Draft"),
        (DRY_RUN, "Dry Run (not posted)"),
        (POSTED, "Posted to SAP"),
        (FAILED, "Failed"),
    ]


class WarehouseMovement(models.Model):
    """
    App-side ledger of every production material movement the wrapper performs,
    mirroring the SAP document it posts (or would post, in dry-run).

    One header per SAP document; `lines` hold the per-item detail. For a
    two-step BH-PM -> BH-PC transfer, the StockTransfer header carries
    `itr_doc_entry` pointing at the InventoryTransferRequest posted first.
    """

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="warehouse_movements"
    )
    movement_type = models.CharField(max_length=20, choices=MovementType.CHOICES)
    status = models.CharField(
        max_length=10, choices=MovementStatus.CHOICES, default=MovementStatus.DRAFT
    )

    from_whs_code = models.CharField(max_length=20, blank=True, default="")
    to_whs_code = models.CharField(max_length=20, blank=True, default="")

    # SAP linkage
    sap_object_type = models.CharField(
        max_length=20, blank=True, default="",
        help_text="SAP object type: 67 transfer, 20 GRPO, 60 issue, etc.",
    )
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.CharField(max_length=30, blank=True, default="")
    itr_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="DocEntry of the Inventory Transfer Request this transfer is based on.",
    )

    posting_date = models.DateField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")
    payload_preview = models.JSONField(
        null=True, blank=True,
        help_text="The Service-Layer payload built for this movement (dry-run visibility).",
    )
    reference = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Optional caller reference (e.g. BOM request id, run id).",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="production_movements",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "movement_type", "status"]),
            models.Index(fields=["sap_object_type", "sap_doc_entry"]),
        ]
        permissions = [
            ("can_create_movement", "Can create warehouse movements (transfers)"),
            ("can_view_movements", "Can view the warehouse movement ledger"),
        ]

    def __str__(self):
        return (
            f"{self.company.code} {self.movement_type} "
            f"{self.from_whs_code or '-'}->{self.to_whs_code or '-'} [{self.status}]"
        )


class WarehouseMovementLine(models.Model):
    movement = models.ForeignKey(
        WarehouseMovement, on_delete=models.CASCADE, related_name="lines"
    )
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=200, blank=True, default="")
    quantity = models.DecimalField(max_digits=19, decimal_places=6)
    uom = models.CharField(max_length=20, blank=True, default="")
    from_whs_code = models.CharField(max_length=20, blank=True, default="")
    to_whs_code = models.CharField(max_length=20, blank=True, default="")
    base_line = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.item_code} x{self.quantity}"
