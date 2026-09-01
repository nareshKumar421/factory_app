# quality_control/models/testing_procedure.py
"""
Controlled testing procedures (SOPs) held as structured data.

A procedure such as "ARGEMONE OIL ADULTERATION TESTING"
(document code ``QA-TST-INH-14-02-10``) is stored as three tables:

* :class:`TestingProcedure`         -- the document header (code, title, revision).
* :class:`TestingProcedureSection`  -- its numbered sections, in order.
* :class:`TestingProcedureLine`     -- the ordered lines inside a section.

The line table is deliberately generic: a bullet (apparatus, reagent,
precaution), a numbered step, and a two-column observation/interpretation
table row are all one row here. ``text`` holds the first column and the
optional ``interpretation`` holds the second. That keeps a procedure of any
shape -- in-house or standard -- round-trippable without a table per section.
"""

from django.db import models

from company.models import Company
from gate_core.models import BaseModel


class ProcedureType(models.TextChoices):
    """The two families of testing procedure the QA team maintains."""

    INHOUSE = "INHOUSE", "In-house Testing Procedure"
    STANDARD = "STANDARD", "Standard Testing Procedure"


class ProcedureStatus(models.TextChoices):
    """Lifecycle of a controlled procedure document."""

    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    ARCHIVED = "ARCHIVED", "Archived"


class ProcedureSectionKey(models.TextChoices):
    """Recognised section headings.

    The parser maps a pasted heading onto one of these so sections mean the
    same thing across procedures. Anything unrecognised is kept verbatim
    under ``OTHER`` rather than dropped.
    """

    SCOPE = "SCOPE", "Scope"
    PRINCIPLE = "PRINCIPLE", "Principle"
    RESPONSIBILITY = "RESPONSIBILITY", "Responsibility"
    APPARATUS = "APPARATUS", "Apparatus / Glassware"
    REAGENT = "REAGENT", "Reagent"
    SAMPLE_REQUIREMENT = "SAMPLE_REQUIREMENT", "Sample Requirement"
    PROCEDURE = "PROCEDURE", "Procedure"
    OBSERVATION = "OBSERVATION", "Observation and Interpretation"
    ACCEPTANCE_CRITERIA = "ACCEPTANCE_CRITERIA", "Acceptance Criteria"
    PRECAUTIONS = "PRECAUTIONS", "Precautions"
    SAFETY = "SAFETY", "Safety"
    CALCULATION = "CALCULATION", "Calculation"
    REFERENCE = "REFERENCE", "Reference"
    OTHER = "OTHER", "Other"


class LineKind(models.TextChoices):
    """What a line inside a section actually is."""

    BULLET = "BULLET", "Bullet"
    STEP = "STEP", "Numbered step"
    TABLE_ROW = "TABLE_ROW", "Table row"
    PARAGRAPH = "PARAGRAPH", "Paragraph"


class TestingProcedure(BaseModel):
    """One controlled testing procedure document."""

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="testing_procedures",
    )

    # --- Identity -------------------------------------------------------
    document_code = models.CharField(
        max_length=64,
        help_text="Controlled document code as printed, e.g. 'QA-TST-INH-14-02-10'.",
    )
    title = models.CharField(
        max_length=255,
        help_text="Procedure title, e.g. 'ARGEMONE OIL ADULTERATION TESTING'.",
    )
    procedure_type = models.CharField(
        max_length=12,
        choices=ProcedureType.choices,
        default=ProcedureType.INHOUSE,
    )
    heading = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Banner line above the title, e.g. 'INHOUSE TESTING PROCEDURE'.",
    )
    organisation = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Issuing entity as printed, e.g. 'JIVO WELLNESS PVT.LTD.'.",
    )

    # --- Revision control (Rule 5 of the document management procedure) ---
    revision_number = models.CharField(
        max_length=8,
        blank=True,
        default="",
        help_text="Revision as printed, e.g. '00'.",
    )
    revision_date = models.DateField(null=True, blank=True)
    total_pages = models.PositiveSmallIntegerField(null=True, blank=True)
    classification = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="e.g. 'Business Confidential'.",
    )

    status = models.CharField(
        max_length=10,
        choices=ProcedureStatus.choices,
        default=ProcedureStatus.ACTIVE,
    )

    # --- Provenance -----------------------------------------------------
    source_text = models.TextField(
        blank=True,
        default="",
        help_text="The raw pasted text this record was parsed from. Kept so a "
        "mis-parse can be re-analysed without re-typing the document.",
    )
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["document_code", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "document_code"],
                name="uq_testing_procedure_company_code",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "procedure_type"]),
            models.Index(fields=["company", "status"]),
        ]
        permissions = [
            ("can_view_testing_procedures", "Can view testing procedures"),
            ("can_manage_testing_procedures", "Can create/edit testing procedures"),
        ]

    def __str__(self):
        return f"{self.document_code} - {self.title}"

    @property
    def revision_label(self):
        """Revision as printed on the document, e.g. '00/15-10-2023'."""
        if self.revision_number and self.revision_date:
            return f"{self.revision_number}/{self.revision_date.strftime('%d-%m-%Y')}"
        return self.revision_number or ""


class TestingProcedureSection(BaseModel):
    """A numbered section of a procedure, e.g. '7. Procedure'."""

    procedure = models.ForeignKey(
        TestingProcedure,
        on_delete=models.CASCADE,
        related_name="sections",
    )
    sequence = models.PositiveSmallIntegerField(
        default=0, help_text="Display order within the procedure."
    )
    section_number = models.CharField(
        max_length=8,
        blank=True,
        default="",
        help_text="Number as printed, e.g. '7'.",
    )
    section_key = models.CharField(
        max_length=24,
        choices=ProcedureSectionKey.choices,
        default=ProcedureSectionKey.OTHER,
    )
    title = models.CharField(
        max_length=255,
        help_text="Heading as printed, e.g. 'Apparatus / Glassware'.",
    )
    body = models.TextField(
        blank=True,
        default="",
        help_text="Prose belonging to the section itself (around its lines).",
    )

    class Meta:
        ordering = ["sequence", "id"]
        indexes = [models.Index(fields=["procedure", "sequence"])]

    def __str__(self):
        return f"{self.procedure.document_code} - {self.section_number}. {self.title}"


class TestingProcedureLine(BaseModel):
    """One ordered line inside a section: bullet, step, or table row."""

    section = models.ForeignKey(
        TestingProcedureSection,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    sequence = models.PositiveSmallIntegerField(default=0)
    kind = models.CharField(
        max_length=12,
        choices=LineKind.choices,
        default=LineKind.BULLET,
    )
    marker = models.CharField(
        max_length=8,
        blank=True,
        default="",
        help_text="Step number or bullet as printed, e.g. '3'.",
    )
    text = models.TextField(help_text="The line, or the first column of a table row.")
    interpretation = models.TextField(
        blank=True,
        default="",
        help_text="Second column of a table row, e.g. the interpretation of an "
        "observation. Blank for bullets and steps.",
    )

    class Meta:
        ordering = ["sequence", "id"]
        indexes = [models.Index(fields=["section", "sequence"])]

    def __str__(self):
        return self.text[:60]
