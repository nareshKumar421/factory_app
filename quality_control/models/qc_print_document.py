# quality_control/models/qc_print_document.py

from django.db import models

from company.models import Company
from gate_core.models import BaseModel


class QCPrintDocument(BaseModel):
    """Company-specific document identifiers shown on QC printouts."""

    class DocumentKey(models.TextChoices):
        RAW_MATERIAL_INSPECTION = (
            "RAW_MATERIAL_INSPECTION",
            "Raw Material Inspection Print",
        )

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="qc_print_documents",
    )
    document_key = models.CharField(max_length=60, choices=DocumentKey.choices)
    document_id = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = ("company", "document_key")
        ordering = ["document_key"]

    def __str__(self):
        return f"{self.get_document_key_display()} - {self.document_id or 'No document ID'}"
