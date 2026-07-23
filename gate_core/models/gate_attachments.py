from django.db import models
from driver_management.models import VehicleEntry

from document_control.models import ControlledDocumentMixin


class GateAttachment(ControlledDocumentMixin, models.Model):
    """
    Model to store attachments related to gate entries
    """

    DOCUMENT_MODULE = "GATE"

    gate_entry = models.ForeignKey(
        VehicleEntry,
        on_delete=models.CASCADE,
        related_name='attachments'
    )
    file = models.FileField(upload_to='gate_attachments/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Attachment for Gate Entry {self.gate_entry.id} - {self.file.name}"