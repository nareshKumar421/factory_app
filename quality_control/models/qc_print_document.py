# quality_control/models/qc_print_document.py

from django.db import models
from django.db.models import Q

from company.models import Company
from gate_core.models import BaseModel


class QCPrintDocument(BaseModel):
    """The document number printed on one of QC's forms, per company.

    Master Data > Print Documents is where every printed form's number lives:
    the two arrival-slip reports (one row per key), and each production QC sheet
    — every production parameter type is its own paper form, so those rows carry
    the type (key PRODUCTION_QC_SHEET).
    """

    class DocumentKey(models.TextChoices):
        RAW_MATERIAL_INSPECTION = (
            "RAW_MATERIAL_INSPECTION",
            "Arrival Slip Inspection Print",
        )
        QC_PARAMETERS = (
            "QC_PARAMETERS",
            "Arrival Slip QC Parameters Print",
        )
        PRODUCTION_QC_SHEET = (
            "PRODUCTION_QC_SHEET",
            "QC Document Sheet",
        )

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="qc_print_documents",
    )
    document_key = models.CharField(max_length=60, choices=DocumentKey.choices)
    # Set exactly when the key is PRODUCTION_QC_SHEET: which form it is.
    production_parameter_type = models.ForeignKey(
        "quality_control.ProductionParameterType",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="print_documents",
    )
    document_id = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["document_key", "production_parameter_type__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "document_key"],
                condition=Q(production_parameter_type__isnull=True),
                name="uq_qc_print_document_company_key",
            ),
            models.UniqueConstraint(
                fields=["company", "production_parameter_type"],
                condition=Q(production_parameter_type__isnull=False),
                name="uq_qc_print_document_company_type",
            ),
            models.CheckConstraint(
                condition=(
                    Q(document_key="PRODUCTION_QC_SHEET", production_parameter_type__isnull=False)
                    | (
                        ~Q(document_key="PRODUCTION_QC_SHEET")
                        & Q(production_parameter_type__isnull=True)
                    )
                ),
                name="ck_qc_print_document_sheet_has_type",
            ),
        ]

    @property
    def label(self):
        if self.production_parameter_type_id:
            return f"Document — {self.production_parameter_type.name}"
        return self.get_document_key_display()

    def __str__(self):
        return f"{self.label} - {self.document_id or 'No document ID'}"
