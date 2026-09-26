"""
SAP Documents — SAP Portal's document browser, merged into JI.

Purchase orders, GRPOs, A/P and A/R invoices and credit notes, returns,
inventory transfers and their requests, journal entries, outgoing payments and
the drafts waiting for approval are read live from SAP (the Service Layer for
the document, HANA for names, journals and attachments — see
``sap_client.hana.document_reader``). None of that is stored here.

What is stored is who downloaded which SAP attachment through this app. The
files are scans of invoices, bills and challans; SAP records nothing when one is
read through the shared service account, so this table is the only trace.

An app of its own rather than part of ``sap_finance``: that app is SAP's
ledgers and budgets, with its own audience; this one is every SAP document and
its attachments, with rights of its own. Company-scoped like every SAP read:
the company is the ``Company-Code`` header's.
"""

from django.db import models

from company.models import Company
from gate_core.models.base import BaseModel


class SapAttachmentDownload(BaseModel):
    """One SAP attachment file served to one person (``created_by``, ``created_at``).

    Written only after the file service returned the file. ``abs_entry`` and
    ``line`` are SAP's own key for the file (``ATC1.AbsEntry`` + ``Line``);
    ``file_name`` is the name it was served under.
    """

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="sap_attachment_downloads"
    )
    abs_entry = models.PositiveIntegerField(help_text="ATC1.AbsEntry — the document's AttachmentEntry.")
    line = models.IntegerField(help_text="ATC1.Line — which file of that entry.")
    file_name = models.CharField(max_length=300, blank=True, default="")
    content_type = models.CharField(max_length=100, blank=True, default="")
    size_bytes = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["company", "abs_entry"])]
        # Only the rights below: no add/change/delete/view rows that nothing
        # checks sitting beside them in the group editor.
        default_permissions = ()
        permissions = [
            ("can_view_sap_documents", "Can browse SAP documents and their attachment lists"),
            ("can_download_sap_attachments", "Can download SAP document attachments"),
        ]

    def __str__(self):
        return f"{self.file_name or 'attachment'} ({self.abs_entry}/{self.line}, {self.company.code})"
