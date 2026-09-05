"""
Department Ownership Chart — who owns each function, and who backs them up.

The chart on the wall ("Purchasing – Oil is Shunty Veerji's, Raspreet /
Lovepreet / Gopi are level-01, the team is level-02") kept as data instead of a
picture, so it can be corrected the day an owner changes rather than the next
time somebody redraws the slide.

Two tables and nothing else:

* :class:`OrgDepartment` — the dark blocks down the left of the chart.
* :class:`OrgFunction`   — one row inside a block: an optional sub-department
  name plus the three levels of people. A department that has no sub-divisions
  (Quality Control, Accounts & HR) carries a single function with a blank name.

The people are plain text, not links to :class:`accounts.User`. The chart names
people the way the factory does ("Shunty Veerji", "Tiwariji", "Team") and lists
collectives that are not user accounts at all, so a free-text list is the honest
shape. It also means the chart never breaks when somebody has no login yet.

These departments are deliberately NOT ``accounts.Department``: that master
carries user assignments and cost rates, while this chart splits and merges
functions for readability ("Dispatch – Docking", "Gupta Godown (Audit)").
Editing the chart must never move a cost rate.
"""

from django.db import models

from gate_core.models.base import BaseModel


class OrgChartPermission(models.Model):
    """Sentinel model carrying the module's permissions (no table of its own)."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_org_chart", "Can view the department ownership chart"),
            ("can_manage_org_chart", "Can edit the department ownership chart"),
        ]


class OrgDepartment(BaseModel):
    """One department block, e.g. "Purchasing" / "Production"."""

    name = models.CharField(max_length=120)
    sort_order = models.PositiveIntegerField(
        default=0, help_text="Position of the block on the chart, top to bottom."
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "org chart department"
        verbose_name_plural = "org chart departments"
        constraints = [
            # Deferred: the page saves the whole chart in one transaction, so a
            # rename that swaps two names must not trip the constraint halfway
            # through the write.
            models.UniqueConstraint(
                fields=["name"],
                name="uniq_org_department_name",
                deferrable=models.Deferrable.DEFERRED,
            )
        ]

    def __str__(self):
        return self.name


class OrgFunction(BaseModel):
    """One row of a department block: a function and the people behind it.

    ``name`` is the sub-department ("Oil", "Packaging Material"). It is blank for
    a department that is not sub-divided — the row then reads as the department
    itself, which is how Quality Control and Accounts & HR appear on the chart.

    The three people fields are ordered lists of names. Empty is meaningful:
    "In & Out" has an owner and no support level, and the chart shows exactly
    that rather than inventing a placeholder.
    """

    department = models.ForeignKey(
        OrgDepartment, on_delete=models.CASCADE, related_name="functions"
    )
    name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Sub-department. Blank for a department that is not sub-divided.",
    )
    owners = models.JSONField(
        default=list, blank=True, help_text="Accountable owner(s) — list of names."
    )
    level_1 = models.JSONField(
        default=list, blank=True, help_text="Level-01 support — list of names."
    )
    level_2 = models.JSONField(
        default=list, blank=True, help_text="Level-02 support — list of names."
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "org chart function"
        verbose_name_plural = "org chart functions"
        constraints = [
            models.UniqueConstraint(
                fields=["department", "name"],
                name="uniq_org_function_name_per_department",
                deferrable=models.Deferrable.DEFERRED,
            )
        ]

    def __str__(self):
        return f"{self.department.name} – {self.name}" if self.name else self.department.name
