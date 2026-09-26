"""
BOM Changes -- a change to a SAP bill of materials, asked for, approved level
by level, then written to SAP.

Ported from SAP Portal's BOM requests (``backend_v1/server.js``
``/api/bom-requests``, stored in its ``ZBOM_REQUESTS`` HANA table). A request is
either a new tree (``CREATE`` -- a ``ProductTrees`` POST) or a replacement of an
existing one (``UPDATE`` -- a PUT of the whole tree, so a line left out is
removed in SAP). The trees themselves live only in SAP; what lives here is the
request, its lines as asked, every decision on it, and what SAP answered.

Scoped by company: every request belongs to the company it was raised in, and
that company's SAP is the one it is written to.

Not ``warehouse.BOMRequest``: that one is production asking the store for the
material a BOM calls for. This app never issues material.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from company.models import Company
from gate_core.models.base import BaseModel

from .constants import (
    ApprovalAction,
    BOMChangeKind,
    BOMChangeStatus,
    BOMType,
    IssueMethod,
    LineType,
)


class BOMChangeRequest(BaseModel):
    """One asked-for change to one BOM. ``created_by`` is the person who asked.

    Columns follow the portal's ``ZBOM_REQUESTS`` (``services/bomRequestStore.js``):
    ``ITEM_CODE`` is the tree's code (a SAP BOM is keyed by its parent item),
    ``COMPONENTS`` became :class:`BOMChangeLine` rows and ``APPROVAL_LOG``
    :class:`BOMChangeApproval` rows. Rows brought over by
    ``import_portal_bom_requests`` carry ``legacy_portal_id`` and name their
    portal people in the ``legacy_*`` text fields; those people have no login
    here, so the foreign keys stay empty.
    """

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="bom_change_requests"
    )
    kind = models.CharField(max_length=10, choices=BOMChangeKind.choices)
    item_code = models.CharField(
        max_length=50, help_text="The parent item, which is also the BOM's code in SAP."
    )
    item_name = models.CharField(max_length=200, blank=True, default="")
    quantity = models.DecimalField(
        max_digits=18, decimal_places=4, default=1,
        help_text="How much of the parent the recipe makes (SAP's tree quantity).",
    )
    bom_type = models.CharField(max_length=20, choices=BOMType.choices, default=BOMType.PRODUCTION)
    warehouse = models.CharField(max_length=20, blank=True, default="")
    distribution_rule = models.CharField(max_length=50, blank=True, default="")
    project = models.CharField(max_length=50, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=BOMChangeStatus.choices, default=BOMChangeStatus.PENDING
    )
    submitted_at = models.DateTimeField(default=timezone.now)

    original_data = models.JSONField(
        null=True, blank=True,
        help_text="UPDATE: the tree as SAP held it, read when the request was raised and "
        "again just before it was replaced.",
    )
    sap_result = models.JSONField(null=True, blank=True, help_text="What SAP answered to the push.")
    sap_pushed_at = models.DateTimeField(null=True, blank=True)
    sap_pushed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bom_changes_pushed",
    )
    # The last push that failed, recorded after the transaction unwound so the
    # rollback cannot erase it. Cleared by a successful push.
    push_error = models.TextField(blank=True, default="")
    push_failed_at = models.DateTimeField(null=True, blank=True)

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bom_changes_cancelled",
    )

    legacy_portal_id = models.PositiveIntegerField(
        null=True, blank=True, unique=True,
        help_text="ZBOM_REQUESTS.ID, for rows imported from SAP Portal.",
    )
    legacy_submitted_by = models.CharField(max_length=160, blank=True, default="")
    legacy_sap_pushed_by = models.CharField(max_length=160, blank=True, default="")

    class Meta:
        ordering = ["-submitted_at", "-id"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "item_code"]),
        ]
        # Only the rights below: no add/change/delete/view rows that nothing
        # checks sitting beside them in the group editor.
        default_permissions = ()
        permissions = [
            ("can_view_bom_changes", "Can view BOM change requests and SAP BOMs"),
            ("can_request_bom_changes", "Can request a new BOM or a change to one"),
            ("can_approve_bom_level_1", "Can approve BOM changes at level 1"),
            ("can_approve_bom_level_2", "Can approve BOM changes at level 2"),
            ("can_push_bom_to_sap", "Can give the final BOM approval that writes SAP"),
            ("can_push_bom_directly", "Can write a BOM change to SAP without approvals"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.item_code} ({self.get_status_display()})"


class BOMChangeLine(models.Model):
    """One component of the tree as the request asks for it."""

    request = models.ForeignKey(BOMChangeRequest, on_delete=models.CASCADE, related_name="lines")
    visual_order = models.PositiveIntegerField(default=0)
    item_type = models.CharField(max_length=10, choices=LineType.choices, default=LineType.ITEM)
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=200, blank=True, default="")
    quantity = models.DecimalField(max_digits=19, decimal_places=6)
    issue_method = models.CharField(
        max_length=10, choices=IssueMethod.choices, default=IssueMethod.MANUAL
    )
    warehouse = models.CharField(max_length=20, blank=True, default="")
    unit_cost = models.DecimalField(max_digits=19, decimal_places=6, default=0)
    comment = models.CharField(max_length=254, blank=True, default="")

    class Meta:
        ordering = ["visual_order", "id"]
        default_permissions = ()

    def __str__(self):
        return f"{self.item_code} x {self.quantity}"


class BOMChangeApproval(models.Model):
    """One approve or reject on a request, at the level it was given.

    ``level`` is the sign-off it was: 1 for the first approval, up to the
    configured number of levels, whose last is the push that wrote SAP.
    ``constants.DIRECT_LEVEL`` (0) is a direct push that skipped them.
    """

    request = models.ForeignKey(
        BOMChangeRequest, on_delete=models.CASCADE, related_name="approvals"
    )
    level = models.PositiveSmallIntegerField()
    from_status = models.CharField(max_length=20, choices=BOMChangeStatus.choices)
    action = models.CharField(max_length=10, choices=ApprovalAction.choices)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bom_change_decisions",
    )
    legacy_decided_by = models.CharField(max_length=160, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["decided_at", "id"]
        default_permissions = ()
        constraints = [
            # SAP Portal's rule (server.js lines 454-458): a person approves a
            # request once, so no one signs two of its levels.
            models.UniqueConstraint(
                fields=["request", "decided_by"],
                # A plain string, so the migration does not import the enum.
                condition=Q(action="APPROVE") & Q(decided_by__isnull=False),
                name="bom_change_one_approval_per_person",
            ),
        ]

    def __str__(self):
        return f"{self.get_action_display()} at level {self.level} on request {self.request_id}"
