"""
Department Ownership Chart — who owns each function, and who backs them up.

The chart on the wall ("Procurement – OIL is Shunty Veerji's section, Raspreet /
Lovepreet / Gopi support him, the team is behind them") kept as data instead of
a picture, so it can be corrected the day an owner changes rather than the next
time somebody redraws the slide.

There is one chart PER COMPANY: Jivo Oil, Jivo Mart and Jivo Beverages are
different plants with different people, and the page shows whichever one the
``Company-Code`` header names. Everything below hangs off a company.

Three tables and nothing else:

* :class:`OrgChartSettings` — the plant the chart is for and who heads it. One
  row per company.
* :class:`OrgDepartment` — the numbered blocks down the left of the chart, each
  with the person who heads the whole department.
* :class:`OrgFunction`   — one row inside a block: the section (with an optional
  second line, "Storage / OIL" vs "Storage / Packing material") plus the three
  levels of people.

The people are plain text, not links to :class:`accounts.User`. The chart names
people the way the factory does ("Shunty Veerji", "Tiwariji", "Team") and lists
collectives that are not user accounts at all, so a free-text list is the honest
shape. It also means the chart never breaks when somebody has no login yet.

These departments are deliberately NOT ``accounts.Department``: that master
carries user assignments and cost rates, while this chart splits and merges
functions for readability ("Despatch / Docking", "Gupta Down"). Editing the
chart must never move a cost rate.
"""

from django.db import models

from company.models import Company
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


class OrgChartSettings(BaseModel):
    """The heading of one company's chart: the plant, and who runs it.

    One row per company. :meth:`load` is the only way it should be fetched: it
    creates the row on first read, so a company whose chart nobody has built yet
    renders a heading rather than a crash.
    """

    company = models.OneToOneField(
        Company, on_delete=models.CASCADE, related_name="org_chart_settings"
    )
    plant_name = models.CharField(
        max_length=120,
        help_text="Title at the top of the chart, e.g. 'Oil Plant'.",
    )
    plant_head = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="Who heads the plant. Blank if the chart does not name one.",
    )

    class Meta:
        verbose_name = "org chart heading"
        verbose_name_plural = "org chart headings"

    @classmethod
    def load(cls, company):
        """This company's heading, created on first read.

        A company nobody has titled yet is called after itself ("Jivo Mart")
        rather than after whichever plant was seeded first.
        """
        settings, _ = cls.objects.get_or_create(
            company=company, defaults={"plant_name": company.name}
        )
        return settings

    def __str__(self):
        return self.plant_name


class OrgDepartment(BaseModel):
    """One numbered department block, e.g. "Procurement" / "Production"."""

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="org_departments"
    )
    name = models.CharField(max_length=120)
    head = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text=(
            "Who heads the whole department, printed under its name. Blank for a "
            "department the chart does not put one person over."
        ),
    )
    sort_order = models.PositiveIntegerField(
        default=0, help_text="Position of the block on the chart, top to bottom."
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "org chart department"
        verbose_name_plural = "org chart departments"
        constraints = [
            # Per company: "Production" is a block on more than one chart.
            # Deferred because the page saves the whole chart in one transaction,
            # so a rename that swaps two names must not trip the constraint
            # halfway through the write.
            models.UniqueConstraint(
                fields=["company", "name"],
                name="uniq_org_department_name",
                deferrable=models.Deferrable.DEFERRED,
            )
        ]

    def __str__(self):
        return f"{self.company.code} – {self.name}"


class OrgFunction(BaseModel):
    """One row of a department block: a section and the people behind it.

    ``name`` is the section ("Storage", "Despatch", "IT"). ``subtitle`` is the
    second line that tells two rows of the same section apart — Storage runs
    once for OIL and once for packing material, and they have different people.
    Both are blank-able: a department that is not sub-divided carries a single
    row with an empty name, which reads as the department itself.

    The three people fields are ordered lists of names. Empty is meaningful:
    "In-Out" has a leader and nobody behind them, and the chart shows exactly
    that rather than inventing a placeholder.
    """

    department = models.ForeignKey(
        OrgDepartment, on_delete=models.CASCADE, related_name="functions"
    )
    name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Section. Blank for a department that is not sub-divided.",
    )
    subtitle = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text=(
            "Second line under the section, telling two rows of the same section "
            "apart (Storage – OIL vs Storage – Packing material)."
        ),
    )
    owners = models.JSONField(
        default=list, blank=True, help_text="Leader (L1) — list of names."
    )
    level_1 = models.JSONField(
        default=list, blank=True, help_text="Supported by (L2) — list of names."
    )
    level_2 = models.JSONField(
        default=list, blank=True, help_text="Team (L3) — list of names."
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "org chart function"
        verbose_name_plural = "org chart functions"
        constraints = [
            # The subtitle is part of the identity: "Storage – OIL" and
            # "Storage – Packing material" are two legitimate rows of one block.
            models.UniqueConstraint(
                fields=["department", "name", "subtitle"],
                name="uniq_org_function_name_per_department",
                deferrable=models.Deferrable.DEFERRED,
            )
        ]

    @property
    def label(self):
        """The section as one line: "Storage – OIL", or just "Storage"."""
        if self.name and self.subtitle:
            return f"{self.name} – {self.subtitle}"
        return self.name or self.subtitle

    def __str__(self):
        return f"{self.department.name} – {self.label}" if self.label else self.department.name
