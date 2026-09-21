"""Which electricity meters a user is the manager of.

The Daily Electricity register had no per-meter ownership: anyone holding
``can_manage_electricity_meter`` could retune any meter on the campus, and
anyone holding ``can_add_daily_electricity`` could file a reading against any of
them. That is fine while one person walks the whole site with a clipboard, and
wrong as soon as each block has its own keeper — the boiler-house keeper should
not be able to change the terrace meter's multiplying factor, nor book a day's
units against it.

This is the mapping that fixes it, deliberately shaped like
``warehouse.UserWarehouse`` (the codebase's existing "which slice of the
business is this user responsible for" table) rather than inventing a new
pattern.

Two things differ from the warehouse version, both because a meter is not a
warehouse:

* A **foreign key**, not a code. Warehouses live in SAP and every model here
  refers to them by string; an :class:`~maintenance.models.ElectricityMeter` is
  a row in this database, so pointing at it is both cheaper and safe against
  renames.
* **No company.** A warehouse code is only unique within a company. A meter is
  factory-wide — several of them feed two companies at once, which is what
  ``ElectricityMeter.companies`` records — so scoping the assignment by company
  would ask an admin to answer a question the hardware does not have an answer
  to.

Read it through :mod:`maintenance.meter_scope`, never directly: the
unrestricted-superuser rule and the "no assignment means no access" rule live
there, and a caller that queries this table itself will miss both.
"""

from django.conf import settings
from django.db import models

from gate_core.models import BaseModel


class UserElectricityMeter(BaseModel):
    """A user manages this electricity meter.

    A user may manage several meters (a block keeper usually covers every meter
    in their block), and a meter may have several managers (shifts, and cover
    for leave). So this is a plain many-to-many with no "primary" flag — nothing
    in the rules needs one, and a primary would only invite code that silently
    ignores the rest.

    ``created_by`` (from :class:`~gate_core.models.BaseModel`) is the
    administrator who made the assignment; the API surfaces it as
    ``assigned_by_name``.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="managed_electricity_meters",
    )
    meter = models.ForeignKey(
        "maintenance.ElectricityMeter",
        on_delete=models.CASCADE,
        related_name="managers",
    )

    class Meta:
        db_table = "maintenance_user_electricity_meter"
        verbose_name = "electricity meter manager"
        verbose_name_plural = "electricity meter managers"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "meter"],
                name="uniq_user_electricity_meter",
            ),
        ]
        indexes = [
            models.Index(fields=["meter", "is_active"]),
            models.Index(fields=["user", "is_active"]),
        ]
        permissions = [
            (
                "can_manage_user_electricity_meters",
                "Can assign users as electricity meter managers",
            ),
        ]
        ordering = ["meter__name", "user__full_name"]

    def __str__(self) -> str:
        return f"{self.user_id} manages meter {self.meter_id}"
