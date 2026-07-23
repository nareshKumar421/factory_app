# quality_control/models/arrival_slip_attachment.py

from django.db import models

from document_control.models import ControlledDocumentMixin

from .material_arrival_slip import MaterialArrivalSlip


class AttachmentType(models.TextChoices):
    CERTIFICATE_OF_ANALYSIS = "CERTIFICATE_OF_ANALYSIS", "Certificate of Analysis"
    CERTIFICATE_OF_QUANTITY = "CERTIFICATE_OF_QUANTITY", "Certificate of Quantity"


class ArrivalSlipAttachment(ControlledDocumentMixin, models.Model):
    """Attachments for Material Arrival Slip (e.g. Certificate of Analysis, Certificate of Quantity)."""

    DOCUMENT_MODULE = "QC"

    arrival_slip = models.ForeignKey(
        MaterialArrivalSlip,
        on_delete=models.CASCADE,
        related_name="attachments"
    )
    file = models.FileField(upload_to="arrival_slip_attachments/")
    attachment_type = models.CharField(
        max_length=30,
        choices=AttachmentType.choices,
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("arrival_slip", "attachment_type")

    def __str__(self):
        return f"{self.get_attachment_type_display()} - Slip #{self.arrival_slip_id}"
