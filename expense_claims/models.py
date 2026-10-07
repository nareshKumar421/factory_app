"""
Expense claims: money somebody in the factory spent and wants approved.

Two people touch a claim:

1. **The submitter** fills in all of it on one page -- which company (the
   page's "Branch"), which SAP business place (the page's "Budget"), which
   SAP G/L account, what it was for, how much, and who approves it -- any
   active user.
2. **The approver** approves or rejects it. A rejection must say why; it goes
   back to the submitter as a notification.

The submitter can change any of it until it is approved. Changing a rejected
expense sends it again.

The budget and the account are *snapshots*: code and name both, as SAP read
when the claim was put in. The list then reads back in full when SAP is down,
and a renamed account does not rewrite what was approved.

Claims are common to every company: one list, whichever company the reader
has selected in the header.

NOTHING IS POSTED TO SAP. SAP supplies the budget and account lists and
nothing else.
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from company.models import Company
from gate_core.models.base import BaseModel


class ExpenseClaimStatus(models.TextChoices):
    """Where a claim has got to."""

    PENDING_APPROVAL = "PENDING_APPROVAL", "Awaiting approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class ExpenseClaim(BaseModel):
    """One expense, from the person who spent it to the person who decides it.

    ``created_by`` (from :class:`BaseModel`) is the submitter, and
    ``created_at`` is when they put it in.
    """

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="expense_claims",
        help_text="The page's 'Branch': Oil, Mart or Beverages, and so whose "
        "SAP the budget and the G/L account were picked from.",
    )

    # --- The page's "Budget": SAP's business place (OBPL) -------------------
    budget_id = models.PositiveIntegerField(help_text="SAP OBPL.BPLId.")
    budget_name = models.CharField(
        max_length=255, help_text="SAP OBPL.BPLName as it read when the claim was put in."
    )

    gl_account_code = models.CharField(max_length=32, help_text="SAP OACT.AcctCode.")
    gl_account_name = models.CharField(
        max_length=255, help_text="SAP OACT.AcctName as it read when the claim was put in."
    )

    comment = models.TextField(
        help_text="What the money was spent on, in the submitter's own words."
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="expense_claims_to_approve",
        help_text="Who it goes to. Only they can decide it.",
    )
    status = models.CharField(
        max_length=20,
        choices=ExpenseClaimStatus.choices,
        default=ExpenseClaimStatus.PENDING_APPROVAL,
    )

    # --- The approver's decision -------------------------------------------
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expense_claims_decided",
    )
    decision_note = models.TextField(
        blank=True, default="", help_text="Required when rejecting."
    )

    class Meta:
        ordering = ["-id"]
        # Nothing gates on the add/change/delete/view rows, and a view_ beside
        # the real right in the group editor is a footgun. Approving needs no
        # right: only to be the person the expense was sent to.
        default_permissions = ()
        permissions = [("can_submit_expense_claim", "Can put in an expense")]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["approver", "status"]),
        ]

    def __str__(self):
        return f"Expense #{self.pk} {self.amount} ({self.status})"
