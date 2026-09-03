# quality_control/models/qc_document_file.py
"""
The PDF library: a controlled document kept as the original file.

Some QC documents are only ever needed exactly as issued -- a signed,
stamped, scanned sheet that must be read as printed rather than re-typed.
This model stores that PDF alongside the three identifiers QA files it by:
document code, title and revision.

It deliberately does *not* use ``document_control.ControlledDocumentMixin``.
That mixin allocates a code from the strict SECTION-DOCTYPE-CC-SS-GG-NN
numbering scheme, but the codes on these sheets are typed in as printed
(e.g. ``QA-TST-INH-14-02-10``, whose ``INH`` segment is outside that scheme).
A plain unique-per-company field matches how the documents are actually
labelled, and is consistent with ``TestingProcedure`` and ``RecordTemplate``.
"""

from django.db import models

from company.models import Company
from gate_core.models import BaseModel

# Reused rather than redefined, so a PDF and a typed-up procedure are filed
# under the very same two types.
from .testing_procedure import ProcedureType


class QCDocumentFile(BaseModel):
    """One controlled document held as its original PDF."""

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="qc_document_files"
    )

    document_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Controlled document code as printed, e.g. "
        "'QA-TST-INH-14-02-10'. Optional -- not every sheet carries one.",
    )
    title = models.CharField(
        max_length=255, help_text="Document title as printed on the sheet."
    )
    revision = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Revision as printed, free text, e.g. '00/15-10-2023' or '01'.",
    )
    procedure_type = models.CharField(
        max_length=12,
        choices=ProcedureType.choices,
        default=ProcedureType.INHOUSE,
        help_text="In-house or standard, matching the INH / STD segment of the "
        "document code.",
    )

    file = models.FileField(upload_to="qc_document_files/")
    original_name = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=100, blank=True, default="")
    file_size = models.PositiveIntegerField(
        null=True, blank=True, help_text="Size in bytes, as uploaded."
    )

    class Meta:
        ordering = ["document_code", "title"]
        constraints = [
            # Conditional: a real code stays unique per company, but any
            # number of documents may be filed without one. A plain unique
            # constraint would treat '' as a value and reject the second
            # code-less upload.
            models.UniqueConstraint(
                fields=["company", "document_code"],
                condition=~models.Q(document_code=""),
                name="uq_qc_document_file_company_code",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "title"]),
            models.Index(fields=["company", "procedure_type"]),
        ]
        permissions = [
            ("can_view_document_files", "Can view the QC PDF document library"),
            ("can_manage_document_files", "Can upload and remove QC PDF documents"),
        ]

    def __str__(self):
        return f"{self.document_code} - {self.title}"
