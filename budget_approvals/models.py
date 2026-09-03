"""
budget_approvals/models.py

No database tables needed — all data is read live from SAP HANA via the
DRAFT_APPROVAL_Budget procedure. This module exists solely to define
custom permissions for the app.
"""

from django.db import models


class BudgetApprovalPermission(models.Model):
    """
    Sentinel model that holds custom permissions for the Budget Approvals
    Dashboard. No database rows are ever written to this table.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_budget_approvals", "Can view Budget Approvals Dashboard"),
        ]
