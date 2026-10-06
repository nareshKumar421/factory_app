"""
amounts_board/models.py

Who answers for each plant's raw material, packing material and finished goods.

WHY NOT THE WAREHOUSE MANAGERS
------------------------------
``warehouse.UserWarehouse`` already says who manages each godown, but it is a
plain many-to-many with no primary: on live (2026-10-06) Oil's BH-PF had five
managers, two of them service logins (BST, Barcode). Picking "the owner" out of
that by value would have named BST as the owner of Oil's finished goods. So the
owner is chosen on purpose, one person per plant per category, and the
managers stay what they are -- listed against each godown in the drill.

The model also carries the board's two rights, so they exist as soon as the
migration runs rather than after a hand-run sync command.
"""

from django.conf import settings
from django.db import models

from company.models import Company
from stock_audit.models import ItemCategory


class StockOwner(models.Model):
    """The one person who answers for a plant's stock of one category."""

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="stock_owners",
    )
    category = models.CharField(
        max_length=5,
        choices=[(c.value, c.label) for c in (ItemCategory.RM, ItemCategory.PM, ItemCategory.FG)],
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_stock_categories",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "amounts_board_stock_owner"
        default_permissions = ()
        permissions = [
            ("can_view_amounts_board", "Can view the Amounts board"),
            ("can_manage_stock_owners", "Can set the RM / PM / FG owners on the Amounts board"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "category"],
                name="amounts_board_one_owner_per_category",
            ),
        ]

    def __str__(self):
        return f"{self.company.code} {self.category}: {self.user}"
