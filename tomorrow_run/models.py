"""Tomorrow's run: the planning sheet, the nightly plan, the picks, the check.

* :class:`PlanningSheet` — the planning team's monthly Excel, as put in. The
  newest one is in charge until a newer one is put in; older ones are kept so
  a plan can always say which sheet it was built from.
* :class:`TomorrowPlan` — one per company and day. ``inputs`` is everything the
  7 pm read found, frozen; ``plan`` is what the engine made of it with the
  day's picks applied. A pick re-runs the engine over the same ``inputs``, so
  the day is re-timed without any number underneath it moving.
* :class:`MachinePick` — "run this first on that machine, because ...". Every
  pick is kept with the three we offered, which is how the plan learns.
* :class:`PlanCheck` — the 7 pm check: last night's plan for today beside what
  the plant really did. It only tells; the plan never reads it.
"""

from django.conf import settings
from django.db import models

from company.models import Company


class PlanningSheet(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="planning_sheets")
    file = models.FileField(upload_to="tomorrow_run/sheets/%Y/%m/")
    file_name = models.CharField(max_length=255)
    tab = models.CharField(max_length=100, blank=True, default="")
    title = models.CharField(max_length=255, blank=True, default="")
    # The sheet's Net Req already took off the stock of this date, so what the
    # plant made counts against it from the day after.
    stock_date = models.DateField()
    date_basis = models.CharField(max_length=100, blank=True, default="")
    header_row = models.PositiveIntegerField(default=0)
    columns = models.JSONField(default=dict, blank=True, help_text="Which sheet column became which field.")
    line_count = models.PositiveIntegerField(default=0)
    net_req_l = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="planning_sheets_put_in",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        default_permissions = ()
        indexes = [models.Index(fields=["company", "-uploaded_at"])]

    def __str__(self) -> str:
        return f"{self.file_name} ({self.stock_date})"


class PlanningSheetLine(models.Model):
    sheet = models.ForeignKey(PlanningSheet, on_delete=models.CASCADE, related_name="lines")
    row = models.PositiveIntegerField()
    code = models.CharField(max_length=50, blank=True, default="")
    name = models.CharField(max_length=255, blank=True, default="")
    # Litres, as the sheet gives them.
    plan_l = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    ecom_l = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    stock_l = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    net_l = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    machine = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        ordering = ["sheet", "row"]
        default_permissions = ()


class TomorrowPlan(models.Model):
    TRIGGER_NIGHTLY = "nightly"
    TRIGGER_MANUAL = "manual"
    TRIGGER_CHOICES = [(TRIGGER_NIGHTLY, "7 pm read"), (TRIGGER_MANUAL, "Read again by hand")]

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="tomorrow_plans")
    for_date = models.DateField()
    read_at = models.DateTimeField()
    sheet = models.ForeignKey(PlanningSheet, on_delete=models.SET_NULL, null=True, blank=True, related_name="plans")
    inputs = models.JSONField(default=dict)
    plan = models.JSONField(default=dict)
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default=TRIGGER_NIGHTLY)
    built_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tomorrow_plans_built",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-for_date"]
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["company", "for_date"], name="uniq_tomorrow_plan_company_day"),
        ]
        permissions = [
            ("can_view_tomorrow_run", "Can view tomorrow's run"),
            ("can_pick_tomorrow_run", "Can pick what runs first on a machine"),
            ("can_manage_tomorrow_run", "Can put in a planning sheet and read the plan again"),
        ]

    def __str__(self) -> str:
        return f"{self.company.code} {self.for_date}"


class MachinePick(models.Model):
    plan = models.ForeignKey(TomorrowPlan, on_delete=models.CASCADE, related_name="picks")
    machine = models.CharField(max_length=40)
    # "<item code>-order", or "other" when none of the offers was right.
    job = models.CharField(max_length=60)
    name = models.CharField(max_length=255, blank=True, default="")
    other = models.CharField(max_length=120, blank=True, default="")
    why = models.TextField()
    rank = models.PositiveSmallIntegerField(null=True, blank=True)
    top3 = models.JSONField(default=list, blank=True, help_text="The three we offered when it was picked.")
    on_plan = models.BooleanField(default=False)
    picked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="machine_picks",
    )
    picked_by_name = models.CharField(max_length=150, blank=True, default="")
    picked_at = models.DateTimeField(auto_now_add=True)
    # "Go back to our suggestion": kept, but no longer applied or learned from.
    cleared_at = models.DateTimeField(null=True, blank=True)
    cleared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="machine_picks_cleared",
    )

    class Meta:
        ordering = ["picked_at", "id"]
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "machine"], condition=models.Q(cleared_at__isnull=True),
                name="uniq_live_pick_per_machine",
            ),
        ]

    def as_pick(self) -> dict:
        return {
            "machine": self.machine, "job": self.job, "why": self.why, "other": self.other,
            "by": self.picked_by_name, "at": self.picked_at.isoformat() if self.picked_at else None,
            "name": self.name, "rank": self.rank,
        }


class PlanCheck(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="tomorrow_plan_checks")
    for_date = models.DateField(help_text="The day that was checked.")
    run_at = models.DateTimeField()
    plan = models.ForeignKey(TomorrowPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name="checks")
    rows = models.JSONField(default=list)
    red = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-for_date"]
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["company", "for_date"], name="uniq_plan_check_company_day"),
        ]
