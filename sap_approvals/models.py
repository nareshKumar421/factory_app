"""
SAP Approvals — SAP Portal's general approval inbox, merged into JI.

Every SAP approval request of every document type that involves the person
looking (raised by them, or waiting on a stage of theirs) is read live from
HANA (``sap_client.hana.approval_inbox_reader``) and decided or withdrawn
through the Service Layer, signed as that person's own SAP account. None of the
requests live here. What does live here is the record of each decision taken
from this app, written after SAP accepted it: SAP stamps the SAP account, this
table says which app user it was, whether they typed their SAP password for it,
and whether they overrode the duplicate-document warning.

An app of its own rather than part of ``warehouse``: the warehouse queues serve
one document family each (transfers, credit notes) and list it company-wide;
this one spans every object type and lists only what involves the caller.
"""

from django.db import models

from company.models import Company
from gate_core.models.base import BaseModel

from .constants import DecisionAction, RejectionCategory


class SapApprovalDecision(BaseModel):
    """One approve, reject or withdraw SAP accepted from this app.

    ``created_by`` (``BaseModel``) is the app user; ``signed_as`` the SAP
    account SAP recorded it against. Never holds a password — ``typed_password``
    only says whether one was typed instead of the stored one. A changed
    decision is a row of its own with ``changed_from`` set; the row it changed
    stays as it was.
    """

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="sap_approval_decisions"
    )
    # OWDD.WddCode and the draft it covered — plain ints: the documents live in SAP.
    wdd_code = models.PositiveIntegerField()
    object_type = models.CharField(max_length=20, blank=True, default="")
    draft_entry = models.PositiveIntegerField(null=True, blank=True)
    action = models.CharField(max_length=10, choices=DecisionAction.choices)
    signed_as = models.CharField(max_length=50, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    typed_password = models.BooleanField(
        default=False, help_text="Signed with a password the user typed, not the stored one."
    )
    confirmed_duplicate = models.BooleanField(
        default=False,
        help_text="Approved although SAP already held a posted copy of the document.",
    )
    # Set when this decision changed one already taken: the request's status
    # just before (APPROVED or REJECTED). Blank for a first decision or a withdraw.
    changed_from = models.CharField(
        max_length=10,
        blank=True,
        default="",
        help_text="The request's earlier outcome, when this decision changed it.",
    )
    # A reject only: what kind of entry it was, for the rejection history.
    category = models.CharField(
        max_length=20, choices=RejectionCategory.choices, blank=True, default=""
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["company", "wdd_code"])]
        # Only the rights below: no add/change/delete/view rows that nothing
        # checks sitting beside them in the group editor.
        default_permissions = ()
        permissions = [
            ("can_view_sap_approval_inbox", "Can view the SAP approvals inbox"),
            ("can_decide_sap_approvals", "Can approve or reject SAP approval requests"),
            ("can_withdraw_own_sap_approvals", "Can withdraw SAP approval requests they raised"),
        ]

    def __str__(self):
        return f"{self.get_action_display()} request {self.wdd_code} as {self.signed_as} ({self.company.code})"
