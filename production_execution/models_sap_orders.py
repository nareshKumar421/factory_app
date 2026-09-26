"""Record of what was done to SAP production orders from this app.

The SAP production-order screens came over from SAP Portal
(``backend_v1/public/production.html``, ``issue-production.html``,
``receipt-production.html``, ``close-production.html``). The orders
themselves live in SAP (``OWOR``) and are read live; the one thing kept here is
who created, released, closed, issued to or received from which order, because
every one of those is posted with the shared Service Layer account and SAP
cannot say which person it was. A row is written only after SAP accepted the
action.

It also carries the screens' rights, so they sit beside the run rights on the
``production_execution`` label without touching ``ProductionRun``.
"""

from django.db import models

from company.models import Company
from gate_core.models.base import BaseModel


class SapProductionOrderAction(BaseModel):
    """One accepted action on a SAP production order, and who took it."""

    class Action(models.TextChoices):
        CREATE = "CREATE", "Created"
        RELEASE = "RELEASE", "Released"
        CLOSE = "CLOSE", "Closed"
        ISSUE = "ISSUE", "Issued for production"
        RECEIPT = "RECEIPT", "Received from production"

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="sap_production_order_actions"
    )
    action = models.CharField(max_length=10, choices=Action.choices)
    order_doc_entry = models.PositiveIntegerField(
        null=True, blank=True, help_text="OWOR.DocEntry of the production order."
    )
    item_code = models.CharField(max_length=50, blank=True, default="")
    quantity = models.DecimalField(max_digits=19, decimal_places=6, null=True, blank=True)
    # The document SAP created for CREATE / ISSUE / RECEIPT (DocEntry, DocNum),
    # or the draft it was held as when an approval procedure caught it.
    sap_doc_entry = models.PositiveIntegerField(null=True, blank=True)
    sap_doc_num = models.PositiveIntegerField(null=True, blank=True)
    pending_approval_draft = models.PositiveIntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["company", "order_doc_entry"])]
        default_permissions = ()
        permissions = [
            ("can_view_sap_production_orders", "Can view SAP production orders"),
            ("can_create_sap_production_orders", "Can create SAP production orders"),
            (
                "can_release_close_sap_production_orders",
                "Can release and close SAP production orders",
            ),
            ("can_issue_for_sap_production_orders", "Can issue materials to SAP production orders"),
            ("can_receive_from_sap_production_orders", "Can receive finished goods from SAP production orders"),
        ]

    def __str__(self):
        return f"{self.get_action_display()} order {self.order_doc_entry or '-'} ({self.company.code})"
