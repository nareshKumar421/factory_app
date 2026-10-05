"""The planning team's monthly plan workbook, as uploaded.

This is NOT SAP's production plan (the OFCT forecast this module reads live).
It is the planning team's own Excel: every SKU's monthly figure split into a
COMMODITY or a PREMIUM block, week by week, plus e-commerce. EXIM kept it
(``planning.PlanningUpload`` / ``PlanningRow``); it moved here with the rest of
EXIM's planning. A month is revised mid-month, so it holds several versions;
the highest is the live one and earlier ones stay for history.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models

from company.models import Company


class MonthlyPlanUpload(models.Model):
    """One uploaded workbook: one version of one month's plan."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="monthly_plan_uploads")
    month = models.DateField(help_text="First day of the planned month.")
    version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=255, blank=True, help_text="The sheet's own banner.")
    source_file = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    #: Who uploaded it, as text: kept for a copied EXIM upload whose uploader
    #: has no login here.
    uploaded_by_label = models.CharField(max_length=150, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    row_count = models.IntegerField(default=0)
    commodity_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    premium_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    ecom_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    #: EXIM's planning_uploads.id, for an upload copied from EXIM.
    exim_id = models.IntegerField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["-month", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["company", "month", "version"], name="pp_monthlyplan_unique_version"),
        ]

    def __str__(self):
        return f"{self.month:%b %Y} v{self.version}"

    def recalculate_totals(self):
        """Roll the row figures up onto the header. Returns self, unsaved."""
        totals = self.rows.aggregate(
            c=models.Sum("commodity_monthly"), p=models.Sum("premium_monthly"),
            e=models.Sum("ecom_planning"), t=models.Sum("total_planning"),
        )
        self.commodity_total = totals["c"] or Decimal("0")
        self.premium_total = totals["p"] or Decimal("0")
        self.ecom_total = totals["e"] or Decimal("0")
        self.grand_total = totals["t"] or Decimal("0")
        self.row_count = self.rows.count()
        return self


class MonthlyPlanRow(models.Model):
    """One SKU line of a plan. The monthly figure sits in one of two blocks,
    COMMODITY or PREMIUM, chosen by the SKU's head; ``total_planning`` is
    commodity + premium + e-commerce, recomputed from the weeks."""

    upload = models.ForeignKey(MonthlyPlanUpload, related_name="rows", on_delete=models.CASCADE)

    code = models.CharField(max_length=50, db_index=True)
    brand = models.CharField(max_length=100, blank=True)
    head = models.CharField(max_length=100, blank=True)
    category = models.CharField(max_length=100, blank=True)
    sub_category = models.CharField(max_length=100, blank=True)
    sku = models.CharField(max_length=255, blank=True)

    per_ltrs = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    ltrs_per_box = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    case_pack = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    commodity_monthly = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    commodity_w1 = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    commodity_w2 = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    commodity_w3 = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    commodity_w4 = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    premium_monthly = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    premium_w1 = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    premium_w2 = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    premium_w3 = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    premium_w4 = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    ecom_planning = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_planning = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    source_row = models.IntegerField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["-total_planning", "code"]

    def __str__(self):
        return f"{self.code} {self.sku}"
