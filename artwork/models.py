"""
The artwork register: what is printed on every label and carton.

One :class:`ArtworkRecord` per SAP item, holding the four things the factory
files artwork by -- the controlled document number, the revision it is at, the
barcode printed on it, and the two files (the print-ready PDF and the
CorelDRAW source the printer works from).

Revisions are the reason the register exists, so a superseded artwork is never
lost: every change writes the previous state to :class:`ArtworkRevision`,
including the file paths. Django does not delete a replaced upload, so the old
PDF and CDR stay on disk and stay downloadable from the history.

Like ``quality_control.QCDocumentFile``, this deliberately does NOT use
``document_control.ControlledDocumentMixin``. That mixin allocates codes from
the strict SECTION-DOCTYPE-CC-SS-GG-NN scheme, whereas an artwork document
number is typed in as printed on the artwork itself.
"""

from django.conf import settings
from django.db import models

from company.models import Company
from gate_core.models import BaseModel

from .constants import ARTWORK_SUB_GROUPS


class ArtworkSubGroup(models.TextChoices):
    """The two artwork-bearing packaging kinds, spelled as SAP spells them."""

    LABEL = "LABEL", "Label"
    CARTON = "CARTON", "Carton"


def _artwork_upload_path(instance, filename):
    """``artwork/<company>/<item>/<filename>``.

    Grouped by item so every revision of one item's artwork sits together on
    disk. Django appends a suffix rather than overwriting when a revision is
    uploaded under a name already taken, which is what keeps the superseded
    file readable from the history.
    """
    company = getattr(getattr(instance, "company", None), "code", "UNKNOWN")
    item = (getattr(instance, "item_code", "") or "UNFILED").replace("/", "-")
    return f"artwork/{company}/{item}/{filename}"


def _revision_upload_path(instance, filename):
    """History rows never receive an upload -- they inherit the record's path.

    A revision's files are the record's previous files, assigned by name rather
    than re-saved, so this is only here to satisfy ``FileField``.
    """
    record = getattr(instance, "record", None)
    company = getattr(getattr(record, "company", None), "code", "UNKNOWN")
    item = (getattr(record, "item_code", "") or "UNFILED").replace("/", "-")
    return f"artwork/{company}/{item}/{filename}"


class ArtworkRecord(BaseModel):
    """The artwork on file for one SAP label or carton item."""

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="artwork_records",
        help_text="The company whose item master this item code comes from. "
        "Item codes repeat across the three schemas and mean different "
        "things, so a record is only ever read back against its own company.",
    )

    # --- Identity, snapshotted from SAP at capture time --------------------
    item_code = models.CharField(
        max_length=50, help_text="SAP OITM.ItemCode, e.g. PM0000086."
    )
    item_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="SAP OITM.ItemName as it read when the artwork was filed. A "
        "snapshot, so the register still names the item if SAP is unreachable; "
        "the live name is shown beside it on the page.",
    )
    sub_group = models.CharField(
        max_length=16,
        choices=ArtworkSubGroup.choices,
        help_text="SAP OITM.U_Sub_Group -- LABEL or CARTON.",
    )

    # --- What is printed on the artwork ------------------------------------
    document_number = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Controlled document number as printed on the artwork. "
        "Typed in, not allocated: artwork numbers are assigned outside this "
        "application. Optional: plenty of artwork reaches the factory before "
        "it has been given a number, and refusing to file it until then would "
        "leave the artwork itself unrecorded.",
    )
    revision_number = models.PositiveSmallIntegerField(
        default=0,
        help_text="Revision as printed, starting at 00. The document number "
        "never changes across a revision; this does.",
    )
    revision_date = models.DateField(
        help_text="Date this revision was issued, as printed on the artwork."
    )
    barcode = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="The barcode printed on the artwork (EAN-13 on a label). "
        "Optional: plain cartons frequently carry none, and refusing to file "
        "an artwork because it has no barcode would leave it unrecorded. SAP "
        "holds no barcode for these items at all -- see constants.py.",
    )

    # --- The files ---------------------------------------------------------
    pdf_file = models.FileField(
        upload_to=_artwork_upload_path, help_text="Print-ready PDF."
    )
    pdf_original_name = models.CharField(max_length=255, blank=True, default="")
    pdf_size = models.PositiveBigIntegerField(null=True, blank=True)

    cdr_file = models.FileField(
        upload_to=_artwork_upload_path, help_text="CorelDRAW (.cdr) source file."
    )
    cdr_original_name = models.CharField(max_length=255, blank=True, default="")
    cdr_size = models.PositiveBigIntegerField(null=True, blank=True)

    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["sub_group", "item_code"]
        constraints = [
            # One live artwork per item. A retired record frees the item so it
            # can be captured again rather than being permanently blocked.
            models.UniqueConstraint(
                fields=["company", "item_code"],
                condition=models.Q(is_active=True),
                name="uq_artwork_company_item",
            ),
            # A controlled document number identifies one document. Retired
            # records are excluded so a number is freed along with its record,
            # and so are unnumbered ones -- "not yet numbered" is not a number,
            # and two of them do not clash.
            models.UniqueConstraint(
                fields=["company", "document_number"],
                condition=models.Q(is_active=True) & ~models.Q(document_number=""),
                name="uq_artwork_company_document_number",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "sub_group"]),
            models.Index(fields=["company", "barcode"]),
        ]
        permissions = [
            ("can_view_artwork", "Can view the label and carton artwork register"),
            ("can_manage_artwork", "Can capture and revise label and carton artwork"),
        ]

    def __str__(self):
        return f"{self.item_code} - {self.document_number} rev {self.revision_label}"

    @property
    def revision_label(self) -> str:
        """Revision zero-padded to two digits, the way it is printed."""
        return f"{self.revision_number:02d}"

    def snapshot_revision(self, user=None) -> "ArtworkRevision":
        """Copy the current state into the history, before it is overwritten.

        The two ``FileField`` values are carried over by *name*: both rows then
        point at the same bytes on disk, and replacing the record's upload
        leaves the history row pointing at the file as it was.
        """
        revision = ArtworkRevision(
            record=self,
            document_number=self.document_number,
            revision_number=self.revision_number,
            revision_date=self.revision_date,
            barcode=self.barcode,
            pdf_original_name=self.pdf_original_name,
            cdr_original_name=self.cdr_original_name,
            remarks=self.remarks,
            superseded_by=user,
        )
        revision.pdf_file.name = self.pdf_file.name or ""
        revision.cdr_file.name = self.cdr_file.name or ""
        revision.save()
        return revision


class ArtworkRevision(models.Model):
    """One superseded state of an artwork record. Never edited, never deleted.

    Written by :meth:`ArtworkRecord.snapshot_revision` immediately before the
    record is changed, so the newest history row is the state just replaced.
    """

    record = models.ForeignKey(
        ArtworkRecord, on_delete=models.CASCADE, related_name="revisions"
    )

    document_number = models.CharField(max_length=64)
    revision_number = models.PositiveSmallIntegerField(default=0)
    revision_date = models.DateField()
    barcode = models.CharField(max_length=64, blank=True, default="")

    pdf_file = models.FileField(upload_to=_revision_upload_path, blank=True)
    pdf_original_name = models.CharField(max_length=255, blank=True, default="")
    cdr_file = models.FileField(upload_to=_revision_upload_path, blank=True)
    cdr_original_name = models.CharField(max_length=255, blank=True, default="")

    remarks = models.TextField(blank=True, default="")

    superseded_at = models.DateTimeField(auto_now_add=True)
    superseded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="superseded_artwork_revisions",
    )

    class Meta:
        ordering = ["-superseded_at", "-id"]
        indexes = [models.Index(fields=["record", "-superseded_at"])]

    def __str__(self):
        return f"{self.record.item_code} rev {self.revision_number:02d} (superseded)"

    @property
    def revision_label(self) -> str:
        return f"{self.revision_number:02d}"


# Re-exported so callers can validate a sub-group without importing constants.
__all__ = [
    "ARTWORK_SUB_GROUPS",
    "ArtworkRecord",
    "ArtworkRevision",
    "ArtworkSubGroup",
]
