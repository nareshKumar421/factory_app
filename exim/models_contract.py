"""Oil contracts: the terms SAP does not keep for a purchase order.

EXIM kept domestic oil contracts twice over: a contract register typed by hand
(``contracts.DomesticReports``, each row a SAP PO number keyed in again) and a
landed-cost sheet uploaded from Excel (``DomesticContractDetails``, "D C.xlsx").
Both went stale. Here a contract IS the SAP purchase order for a raw-material
oil, read live, and every truck against it is its SAP GRPO or, before that, its
gate entry (``exim.services_contract``).

What SAP does not hold is how the oil is delivered and what it costs to bring
in: whether the supplier delivers (FOR) or we collect (EXW), and the freight
and brokerage per tonne on an EXW contract. EXIM's sheet carried those per
load, but they are the contract's: every load of one PO had the same rates.
They are kept here, one row per PO, and are what a landed cost adds to the
oil's price.
"""

from django.conf import settings
from django.db import models

from company.models import Company


class DeliveryTerms(models.TextChoices):
    #: The supplier delivers to the factory; the price includes the freight.
    FOR = "FOR", "FOR (delivered)"
    #: We collect from the supplier's works and pay the truck.
    EXW = "EXW", "EXW (we collect)"


class ContractTerms(models.Model):
    """The delivery terms, freight and brokerage of one SAP purchase order."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_contract_terms")
    #: SAP's PO number (DocNum), as the gate, the GRPO and EXIM all quote it.
    po_number = models.CharField(max_length=30)
    delivery_terms = models.CharField(max_length=3, choices=DeliveryTerms.choices, blank=True)
    #: Rupees per tonne unloaded, on an EXW contract.
    freight_per_mt = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    #: Rupees per tonne loaded.
    brokerage_per_mt = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.CharField(max_length=255, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)
    #: Set when the row was seeded from EXIM's contracts or its DC sheet; a row
    #: changed here since is left alone by a later copy.
    copied_from_exim_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["-po_number"]
        constraints = [
            models.UniqueConstraint(fields=["company", "po_number"], name="exim_contractterms_unique_po"),
        ]

    def __str__(self) -> str:
        return f"PO {self.po_number} {self.delivery_terms}"
