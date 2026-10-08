# vehicle_management/models/transporter.py

from django.db import models
from gate_core.models import BaseModel


class Transporter(BaseModel):
    name = models.CharField(max_length=150, unique=True)
    contact_person = models.CharField(max_length=100, blank=True)
    mobile_no = models.CharField(max_length=15, blank=True)
    gstin = models.CharField(max_length=20, blank=True, default="")

    def __str__(self):
        return self.name


class TransporterSAPLink(BaseModel):
    """A transporter's vendor record in one company's SAP.

    SAP keeps a vendor master per company, so one transporter is up to three
    codes (Abhiman Express is VENDA001676 in Oil, VENDA001019 in Mart and
    VENDA001362 in Beverages), while every company's trucks share the one
    Transporter row. A transporter typed by hand has no link at all.

    Several transporters may point at the same code: the app holds duplicates
    ("Echo plast", "Echo plast india", "ECHO PLAST INDIA") that were linked
    rather than merged.
    """

    transporter = models.ForeignKey(
        Transporter, on_delete=models.CASCADE, related_name="sap_links"
    )
    company = models.ForeignKey(
        "company.Company", on_delete=models.CASCADE, related_name="transporter_sap_links"
    )
    card_code = models.CharField(max_length=50)
    # SAP's name and GSTIN when the link was made. The GSTIN is what finds the
    # same transporter again when it is picked in another company.
    card_name = models.CharField(max_length=150, blank=True, default="")
    gstin = models.CharField(max_length=20, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["transporter", "company", "card_code"],
                name="uniq_transporter_sap_link",
            ),
        ]
        indexes = [models.Index(fields=["company", "card_code"])]

    def __str__(self):
        return f"{self.transporter} = {self.card_code} ({self.company_id})"
