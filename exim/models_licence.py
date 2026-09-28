"""Export licences: the duty-free import schemes the company holds.

EXIM kept two of them, with a table pair each:

* **Advance Authorisation.** Import duty free first (every bill of entry is an
  import line), then meet the export obligation that import creates (every
  shipping bill is an export line).
* **DFIA** (Duty Free Import Authorisation). Export first, and the exports earn
  an entitlement to import duty free.

They are the same record read in opposite directions, so here they share one
model: a licence has lines, each line is an import (a bill of entry) or an
export (a shipping bill), and the kind says which direction comes first. What
the first leg creates is the licence's obligation (Advance: exports still owed)
or entitlement (DFIA: imports still allowed), and the balance is that less what
the second leg has used. The arithmetic lives in ``exim.services_licence``.

The totals are stored, not derived on read, for the reason EXIM stored them:
they are the figures the licence has been reported against. Every line write
recalculates them. A licence copied from EXIM keeps EXIM's figures until one of
its lines changes, even where EXIM's own arithmetic had drifted from them.
"""

from django.conf import settings
from django.db import models

from company.models import Company


class LicenceKind(models.TextChoices):
    ADVANCE = "ADVANCE", "Advance Authorisation"
    DFIA = "DFIA", "DFIA"


class LicenceStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    CLOSED = "CLOSED", "Closed"


class LineDirection(models.TextChoices):
    #: A bill of entry.
    IMPORT = "IMPORT", "Import"
    #: A shipping bill.
    EXPORT = "EXPORT", "Export"


def _money():
    return models.DecimalField(max_digits=18, decimal_places=3)


def _rate():
    return models.DecimalField(max_digits=10, decimal_places=3)


def _tonnes(**kwargs):
    return models.DecimalField(max_digits=14, decimal_places=3, **kwargs)


class Licence(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_licences")
    kind = models.CharField(max_length=10, choices=LicenceKind.choices)
    #: The licence number on an Advance Authorisation, the file number on a DFIA.
    number = models.CharField(max_length=50)
    status = models.CharField(max_length=10, choices=LicenceStatus.choices, default=LicenceStatus.OPEN)

    issue_date = models.DateField()
    import_validity = models.DateField()
    export_validity = models.DateField()

    cif_value_inr = _money()
    cif_exchange_rate = _rate()
    #: CIF in INR over its exchange rate; worked out on save, never entered.
    cif_value_usd = _money()
    fob_value_inr = _money()
    fob_exchange_rate = _rate()
    #: FOB in INR over its exchange rate; worked out on save, never entered.
    fob_value_usd = _money()

    #: The quantity the licence authorises for its first leg, as issued: EXIM's
    #: "Valid Import" on an Advance Authorisation, "Valid Export" on a DFIA.
    authorised_qty_mts = _tonnes(default=0)

    # --- figures kept up to date from the lines ----------------------------
    total_import_mts = _tonnes(default=0)
    total_export_mts = _tonnes(default=0)
    #: What the first leg created: exports still owed (Advance) or imports
    #: allowed (DFIA).
    obligation_mts = _tonnes(default=0)
    #: The obligation less the second leg so far. Null only on a licence copied
    #: from EXIM before EXIM worked one out.
    balance_mts = _tonnes(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: ``"<KIND>:<number>"`` for a licence copied from EXIM, so a re-run of the
    #: copy finds it again. Null for one raised here.
    exim_ref = models.CharField(max_length=80, null=True, blank=True, unique=True)
    #: When the copy last wrote it. A licence changed here after that is left
    #: alone by a later copy.
    copied_from_exim_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["-issue_date", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "kind", "number"], name="exim_licence_unique_number",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.number}"

    @property
    def first_leg(self) -> str:
        """The direction that creates the obligation."""
        return LineDirection.IMPORT if self.kind == LicenceKind.ADVANCE else LineDirection.EXPORT

    @property
    def second_leg(self) -> str:
        """The direction that uses it up."""
        return LineDirection.EXPORT if self.kind == LicenceKind.ADVANCE else LineDirection.IMPORT


class LicenceLine(models.Model):
    licence = models.ForeignKey(Licence, on_delete=models.CASCADE, related_name="lines")
    direction = models.CharField(max_length=10, choices=LineDirection.choices)
    #: The bill of entry number (import) or shipping bill number (export).
    document_no = models.CharField(max_length=50)
    #: Always given for a bill of entry; a shipping bill may not carry one yet.
    document_date = models.DateField(null=True, blank=True)
    value_usd = _money()
    quantity_mts = _tonnes()
    #: A second-leg line may name the first-leg line it answers: the bill of
    #: entry an Advance export discharges, the shipping bill a DFIA import is
    #: made against. Removing that line keeps this one and drops the link.
    linked_line = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="linked_from",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: ``"<EXIM table>:<id>"`` for a line copied from EXIM. Null for one entered here.
    exim_ref = models.CharField(max_length=80, null=True, blank=True, unique=True)

    class Meta:
        default_permissions = ()
        ordering = ["document_date", "id"]

    def __str__(self) -> str:
        return f"{self.get_direction_display()} {self.document_no}"
