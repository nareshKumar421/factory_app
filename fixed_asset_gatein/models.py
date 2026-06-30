from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from driver_management.models import VehicleEntry
from gate_core.models import UnitChoice


class AssetCategory(models.Model):
    """
    Lookup table for fixed asset categories.
    Examples: Machinery, Computers, Furniture, Vehicles, Tools, etc.
    """
    category_name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category_name"]
        verbose_name = "Asset Category"
        verbose_name_plural = "Asset Categories"
        permissions = [
            ("can_manage_asset_category", "Can manage asset category"),
        ]

    def __str__(self):
        return self.category_name


class FixedAssetGateEntry(models.Model):
    """
    Fixed Asset inward gate pass entry (shipment header).
    Linked to VehicleEntry via a OneToOne relationship (company scoping is
    inherited from the parent VehicleEntry). One entry can carry multiple
    assets via the related FixedAssetItem rows.
    """

    # Parent - OneToOne with VehicleEntry
    vehicle_entry = models.OneToOneField(
        VehicleEntry,
        on_delete=models.CASCADE,
        related_name="fixed_asset_entry"
    )

    # Work Order Number (auto-generated: FA-YYYY-NNN)
    work_order_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        unique=True
    )

    # Supplier / Source
    supplier_name = models.CharField(max_length=200)
    invoice_number = models.CharField(max_length=100, blank=True, null=True)

    # Notes
    remarks = models.TextField(blank=True, null=True)

    # Inward Time
    inward_time = models.DateTimeField(auto_now_add=True)

    # Audit
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="fixed_asset_entries"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Fixed Asset Gate Entry"
        verbose_name_plural = "Fixed Asset Gate Entries"
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["work_order_number"]),
        ]
        permissions = [
            ("can_complete_fixed_asset_entry", "Can complete fixed asset gate entry"),
        ]

    def __str__(self):
        return f"{self.supplier_name} ({self.work_order_number})"

    def save(self, *args, **kwargs):
        # Auto-generate work_order_number if not provided
        if not self.work_order_number:
            self.work_order_number = self._generate_work_order_number()
        super().save(*args, **kwargs)

    def _generate_work_order_number(self):
        """Generate work order number in format: FA-YYYY-NNN"""
        from django.utils import timezone
        year = timezone.now().year

        last_entry = FixedAssetGateEntry.objects.filter(
            work_order_number__startswith=f"FA-{year}-"
        ).order_by("-work_order_number").first()

        if last_entry and last_entry.work_order_number:
            try:
                last_number = int(last_entry.work_order_number.split("-")[-1])
                new_number = last_number + 1
            except (ValueError, IndexError):
                new_number = 1
        else:
            new_number = 1

        return f"FA-{year}-{new_number:03d}"


class FixedAssetItem(models.Model):
    """A single asset line within a FixedAssetGateEntry (multiple per vehicle)."""

    fixed_asset_entry = models.ForeignKey(
        FixedAssetGateEntry,
        on_delete=models.CASCADE,
        related_name="items"
    )
    line_no = models.PositiveIntegerField(default=1)

    asset_category = models.ForeignKey(
        AssetCategory,
        on_delete=models.SET_NULL,
        null=True,
        related_name="asset_items"
    )
    asset_name = models.CharField(max_length=200)
    serial_number = models.CharField(max_length=150, blank=True, null=True)

    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0.01, message="Quantity must be positive")]
    )
    unit = models.ForeignKey(
        UnitChoice,
        on_delete=models.SET_NULL,
        null=True,
        related_name="fixed_asset_items"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["line_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["fixed_asset_entry", "line_no"],
                name="unique_fixed_asset_entry_line"
            ),
        ]

    def __str__(self):
        return f"{self.asset_name} ({self.quantity} {self.unit})"
