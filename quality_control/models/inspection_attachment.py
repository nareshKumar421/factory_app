# quality_control/models/inspection_attachment.py

from django.conf import settings
from django.db import models

from .raw_material_inspection import RawMaterialInspection

User = settings.AUTH_USER_MODEL


class InspectionAttachment(models.Model):
    """Files uploaded during QC inspection."""

    inspection = models.ForeignKey(
        RawMaterialInspection,
        on_delete=models.CASCADE,
        related_name="qc_attachments",
    )
    file = models.FileField(upload_to="qc_inspection_attachments/")
    original_name = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qc_inspection_attachments",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        name = self.original_name or self.file.name
        return f"{name} - Inspection #{self.inspection_id}"
