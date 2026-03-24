from django.db import models
from django.conf import settings
from gate_core.models.base import BaseModel


class GoodsIssueStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    POSTED = "POSTED", "Posted to SAP"
    FAILED = "FAILED", "Failed"


class GoodsIssuePosting(BaseModel):
    """
    Tracks Goods Issue postings to SAP for dispatched shipments.
    Created when a shipment is sealed and dispatched.
    """
    shipment_order = models.OneToOneField(
        "outbound_dispatch.ShipmentOrder",
        on_delete=models.CASCADE,
        related_name="goods_issue"
    )

    # SAP Response
    sap_doc_entry = models.IntegerField(null=True, blank=True, help_text="SAP Goods Issue DocEntry")
    sap_doc_num = models.IntegerField(null=True, blank=True, help_text="SAP Goods Issue DocNum")

    status = models.CharField(
        max_length=20,
        choices=GoodsIssueStatus.choices,
        default=GoodsIssueStatus.PENDING
    )

    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="goods_issue_postings"
    )

    error_message = models.TextField(blank=True, default="")
    retry_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        permissions = [
            ("can_post_goods_issue", "Can post goods issue to SAP"),
            ("can_retry_goods_issue", "Can retry failed goods issue"),
        ]

    def __str__(self):
        return f"GI-{self.sap_doc_num or 'PENDING'} for {self.shipment_order}"
