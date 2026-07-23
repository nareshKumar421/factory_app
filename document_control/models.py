"""
Registry model + reusable mixin for controlled documents.

``DocumentCode`` is the single source of truth for uniqueness and sequential
numbering. Every issued code is one row here (one row == one controlled
document). ``ControlledDocumentMixin`` is inherited by any record (e.g. a
GATE / QC / GRPO file attachment) that must carry a controlled-document code.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from . import numbering


class DocumentCode(models.Model):
    """A single controlled-document code issued under DOC-SOP-04-02-00-01.

    Uniqueness is guaranteed two ways: the ``code`` string is unique, and the
    (section, doctype, cc, ss, gg, nn) tuple is unique -- so the same serial can
    never be issued twice within a numbering group.
    """

    # The rendered code, e.g. "STR-FRM-08-05-00-01". Unique + immutable.
    code = models.CharField(max_length=64, unique=True, editable=False)

    # Structured parts (denormalised for querying / next-serial lookups).
    section = models.CharField(max_length=8)
    doctype = models.CharField(max_length=8)
    cc = models.CharField(max_length=2)
    ss = models.CharField(max_length=2)
    gg = models.CharField(max_length=2)
    nn = models.PositiveSmallIntegerField(
        help_text="Sequential serial within the SECTION+DOCTYPE+clause group.",
    )

    # Rule 5 -- every document record stores revision, issue date & pages.
    revision_number = models.PositiveSmallIntegerField(
        default=0,
        help_text="Revision number, starts at 00. A revision bumps this; the "
        "code itself never changes.",
    )
    issue_date = models.DateField(help_text="Revision / issue date.")
    total_pages = models.PositiveIntegerField(
        default=1, help_text="Number of pages in the controlled document."
    )

    # Provenance -- which module / object this code was issued for.
    module = models.CharField(max_length=16, blank=True)
    source_reference = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issued_document_codes",
    )

    class Meta:
        ordering = ["section", "doctype", "cc", "ss", "gg", "nn"]
        constraints = [
            models.UniqueConstraint(
                fields=["section", "doctype", "cc", "ss", "gg", "nn"],
                name="uq_documentcode_group_serial",
            ),
        ]
        indexes = [
            models.Index(fields=["section", "doctype", "cc", "ss", "gg"]),
        ]

    def __str__(self) -> str:
        return self.code

    @property
    def clause(self) -> str:
        return f"{self.cc}-{self.ss}-{self.gg}"

    @property
    def revision_label(self) -> str:
        """Revision zero-padded to two digits, e.g. '00'."""
        return f"{self.revision_number:02d}"

    @property
    def issue_date_display(self) -> str:
        """Issue date as DD-MM-YYYY per the procedure."""
        return self.issue_date.strftime("%d-%m-%Y") if self.issue_date else ""

    def bump_revision(self, *, issue_date=None, save=True):
        """Record a revision: increment the revision number, keep the code.

        Rule 5: "A revision bumps the revision number, never changes the
        document code."
        """
        self.revision_number = models.F("revision_number") + 1
        if issue_date is not None:
            self.issue_date = issue_date
        if save:
            self.save(update_fields=["revision_number", "issue_date", "updated_at"])
            self.refresh_from_db(fields=["revision_number"])
        return self


class ControlledDocumentMixin(models.Model):
    """Abstract mixin: give any record a controlled-document code.

    Subclasses set :attr:`DOCUMENT_MODULE` to a key in
    ``config.MODULE_DOCUMENT_TYPES`` so the write path knows which numbering
    group to allocate from.

    The field is nullable at the database level (legacy rows predate the
    procedure), but a *newly created* record is refused unless it carries a
    code -- see :meth:`save` -- so a PDF can never be persisted without a
    valid, unique document code.
    """

    #: Overridden by each concrete model (e.g. "GATE", "QC", "GRPO").
    DOCUMENT_MODULE = None

    document_code = models.OneToOneField(
        DocumentCode,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Controlled-document code assigned to this file.",
    )

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.document_code_id is None:
            raise ValidationError(
                {"document_code": "A valid controlled-document code is required."}
            )

    def save(self, *args, **kwargs):
        # Refuse to create a new controlled record without a code. Updates to
        # pre-existing (legacy) rows are left alone.
        if self._state.adding and self.document_code_id is None:
            raise ValidationError(
                "Cannot save this document without a controlled-document code. "
                "Allocate one via document_control.services first."
            )
        super().save(*args, **kwargs)

    # -- convenience accessors (safe on legacy rows without a code) ----------
    @property
    def document_code_str(self) -> str:
        return self.document_code.code if self.document_code_id else ""

    @property
    def revision_label(self) -> str:
        return self.document_code.revision_label if self.document_code_id else ""

    @property
    def issue_date_display(self) -> str:
        return self.document_code.issue_date_display if self.document_code_id else ""
