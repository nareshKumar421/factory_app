from django.conf import settings
from django.db import models
from driver_management.models import VehicleEntry

from document_control.models import ControlledDocumentMixin


class GateAttachment(ControlledDocumentMixin, models.Model):
    """
    Model to store attachments related to gate entries.

    Attachments are never physically deleted: a wrong upload (e.g. the wrong
    weighbridge slip) is *soft-removed* by flipping ``is_active`` to False and
    recording who removed it and when. The file itself is retained so it stays
    openable from the audit trail, and callers that gate on "a document was
    uploaded" must therefore filter on ``is_active=True``.
    """

    DOCUMENT_MODULE = "GATE"

    gate_entry = models.ForeignKey(
        VehicleEntry,
        on_delete=models.CASCADE,
        related_name='attachments'
    )
    file = models.FileField(upload_to='gate_attachments/')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='gate_attachments_uploaded',
    )

    # Soft-delete: a removed attachment is kept on disk and in the table so the
    # audit trail can still surface it; it is just excluded from the live list.
    is_active = models.BooleanField(default=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='gate_attachments_removed',
    )
    remove_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['uploaded_at', 'id']

    def __str__(self):
        return f"Attachment for Gate Entry {self.gate_entry.id} - {self.file.name}"