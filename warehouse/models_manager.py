"""Which warehouses a user is the manager of.

The app had no per-user warehouse scoping: anyone holding the transfer or BST
permission could raise a movement out of any warehouse and accept one into any
warehouse. That is fine while a handful of people run every dock, and wrong as
soon as each site has its own manager — a Bhakharpur manager should not be able
to empty the Mayapuri floor, nor accept goods on its behalf.

This is the mapping that fixes it, deliberately shaped like `company.UserCompany`
(the codebase's existing "which slice of the business is this user in" table)
rather than inventing a new pattern.

Warehouse **codes** are stored, not foreign keys, because warehouses live in SAP
(`OWHS`) and every other model here refers to them by code — `BSTTransfer
.sap_from_warehouse`, `WarehouseTransferRequest.from_warehouse`. A code is only
unique within a company, hence the company on every row.

Read it through `warehouse.services.warehouse_scope`, never directly: the
unrestricted-superuser rule and the "no assignment means no access" rule live
there, and a caller that queries this table itself will miss both.
"""

from django.conf import settings
from django.db import models

from company.models import Company


class UserWarehouse(models.Model):
    """A user manages this warehouse, in this company.

    A user may manage several warehouses (a site manager often covers the
    finished-goods and packing-material floors), and a warehouse may have
    several managers (shifts, and cover for leave). So this is a plain
    many-to-many with no "primary" flag — nothing in the rules needs one, and a
    primary would only invite code that silently ignores the rest.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="managed_warehouses",
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="warehouse_managers",
    )
    # 50 to match BSTTransfer.sap_from_warehouse, the longest of the existing
    # warehouse-code columns.
    warehouse_code = models.CharField(max_length=50, db_index=True)

    # Deactivating beats deleting when someone changes site: the row stays as a
    # record of who was responsible when a past transfer was raised.
    is_active = models.BooleanField(default=True)

    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_assignments_made",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_user_warehouse"
        verbose_name = "warehouse manager"
        verbose_name_plural = "warehouse managers"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "company", "warehouse_code"],
                name="uniq_user_company_warehouse",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "warehouse_code"]),
            models.Index(fields=["user", "company"]),
        ]
        permissions = [
            (
                "can_manage_user_warehouses",
                "Can assign users as warehouse managers",
            ),
        ]
        ordering = ["company__code", "warehouse_code", "user__full_name"]

    def __str__(self) -> str:
        return f"{self.user_id} manages {self.warehouse_code} ({self.company_id})"

    def save(self, *args, **kwargs):
        # SAP warehouse codes are upper case; a lower-case assignment would
        # never match `BSTTransfer.sap_from_warehouse` and would read as "the
        # page saved but the restriction does nothing".
        self.warehouse_code = (self.warehouse_code or "").strip().upper()
        super().save(*args, **kwargs)
