"""
SAP Finance — the finance screens that came over from SAP Portal.

Journal entries, the general ledger of one account and the chart of accounts
are read live from SAP HANA (``sap_client.hana.finance_reader``); the budget
screen reads and writes SAP's ``BUDGET`` user-defined object through the
Service Layer (``sap_client.service_layer.budget_writer``). None of that data
lives here. What does live here is the record of every budget change made from
this app, because SAP stamps only the shared service account on a UDO write —
this table says which person it was.

An app of its own rather than part of ``budget_approvals``: that app is a
read-only dashboard of draft lines against budget heads, mounted under
``dashboards/`` with a view-only right; this one writes to SAP and carries the
ledger reads the portal grouped with it.
"""

from django.db import models

from company.models import Company
from gate_core.models.base import BaseModel

from .constants import BudgetAction


class SapBudgetChange(BaseModel):
    """One create, update or delete of a SAP ``BUDGET`` document from this app.

    Written only after SAP accepted the change. ``payload`` is exactly what was
    sent (budget codes, months, amounts — nothing secret), so an audit can see
    the change without SAP's own history.
    """

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="sap_budget_changes"
    )
    action = models.CharField(max_length=10, choices=BudgetAction.choices)
    doc_entry = models.PositiveIntegerField(
        null=True, blank=True, help_text="The BUDGET document's DocEntry in SAP."
    )
    budget_code = models.CharField(max_length=50, blank=True, default="")
    sub_budget_code = models.CharField(max_length=50, blank=True, default="")
    line_count = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["company", "doc_entry"])]
        # Only the rights below: no add/change/delete/view rows that nothing
        # checks sitting beside them in the group editor.
        default_permissions = ()
        permissions = [
            (
                "can_view_sap_ledgers",
                "Can view SAP journal entries, general ledger and chart of accounts",
            ),
            ("can_view_sap_budgets", "Can view SAP budgets"),
            ("can_manage_sap_budgets", "Can create, edit and delete SAP budgets"),
        ]

    def __str__(self):
        return f"{self.get_action_display()} budget {self.doc_entry or '-'} ({self.company.code})"
