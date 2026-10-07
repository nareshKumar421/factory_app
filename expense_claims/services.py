"""
Everything that moves an expense claim goes through here.
"""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from cash_book.hana_reader import GLAccountReader
from company.models import Company
from sap_client.exceptions import SAPDataError

from . import notifications
from .constants import COMPANY_LABELS
from .hana_reader import BranchReader
from .models import ExpenseClaim, ExpenseClaimStatus


def companies():
    """Oil, Mart and Beverages -- the page's "Branch" choices -- in that order."""
    found = {c.code: c for c in Company.objects.filter(code__in=COMPANY_LABELS, is_active=True)}
    return [found[code] for code in COMPANY_LABELS if code in found]


def company_by_code(code):
    """One of :func:`companies`, or a 400 naming the field."""
    for company in companies():
        if company.code == (code or "").strip():
            return company
    raise ValidationError({"company": "Pick Oil, Mart or Beverages."})


def approvers():
    """Everyone an expense may go to: every active user.

    People kept only to hold cash (the cash book's drivers and tradesmen) are
    made inactive precisely because they cannot sign in, so they are not here.
    """
    return get_user_model().objects.filter(is_active=True).order_by("full_name", "email")


def _checked(*, user, company, budget_id, gl_account_code, comment, approver):
    """Everything an expense is filed under, confirmed against the company's SAP.

    The budget and the account are confirmed against the chosen company's SAP
    and their names snapshotted. SAP being unreachable raises
    ``SAPConnectionError`` for the view to answer 503 -- the pickers this is
    fed from read SAP too, so trying again is the answer.
    """
    comment = (comment or "").strip()
    if not comment:
        raise ValidationError({"comment": "Say what the money was spent on."})
    if approver.pk == user.pk:
        raise ValidationError({"approver": "Your own expense has to go to somebody else."})

    try:
        budget = BranchReader(company.code).resolve(budget_id)
    except SAPDataError as exc:
        raise ValidationError({"budget_id": str(exc)}) from exc
    try:
        account = GLAccountReader(company.code).resolve((gl_account_code or "").strip())
    except SAPDataError as exc:
        raise ValidationError({"gl_account_code": str(exc)}) from exc

    return {
        "company": company,
        "budget_id": budget["branch_id"],
        "budget_name": budget["branch_name"],
        "gl_account_code": account["account_code"],
        "gl_account_name": account["account_name"],
        "comment": comment,
        "approver": approver,
    }


@transaction.atomic
def submit(*, user, company, budget_id, gl_account_code, comment, amount, approver):
    """Put an expense in, and send it to its approver."""
    fields = _checked(
        user=user,
        company=company,
        budget_id=budget_id,
        gl_account_code=gl_account_code,
        comment=comment,
        approver=approver,
    )
    claim = ExpenseClaim.objects.create(
        **fields, amount=amount, created_by=user, updated_by=user
    )
    transaction.on_commit(lambda: notifications.sent_to_hod(claim, actor=user))
    return claim


@transaction.atomic
def edit(*, user, claim_id, company, budget_id, gl_account_code, comment, amount, approver):
    """Change an expense you put in, any time before it is approved.

    Editing a rejected one sends it again: it goes back to awaiting approval
    and the old verdict is cleared. Whoever it now waits on is told -- unless
    it was already waiting on them, when a correction is not news.
    """
    try:
        claim = ExpenseClaim.objects.select_for_update().get(pk=claim_id, is_active=True)
    except ExpenseClaim.DoesNotExist:
        raise NotFound("No such expense.")
    if claim.created_by_id != user.pk:
        raise PermissionDenied("You can only change an expense you put in yourself.")
    if claim.status == ExpenseClaimStatus.APPROVED:
        raise ValidationError({"detail": "This expense is approved and can no longer be changed."})

    fields = _checked(
        user=user,
        company=company,
        budget_id=budget_id,
        gl_account_code=gl_account_code,
        comment=comment,
        approver=approver,
    )
    was_waiting_on = (
        claim.approver_id if claim.status == ExpenseClaimStatus.PENDING_APPROVAL else None
    )

    for name, value in fields.items():
        setattr(claim, name, value)
    claim.amount = amount
    claim.status = ExpenseClaimStatus.PENDING_APPROVAL
    claim.decided_at = None
    claim.decided_by = None
    claim.decision_note = ""
    claim.updated_by = user
    claim.save()

    if claim.approver_id != was_waiting_on:
        transaction.on_commit(lambda: notifications.sent_to_hod(claim, actor=user))
    return claim


@transaction.atomic
def decide(*, user, claim_id, approve: bool, note=""):
    """The approver's verdict. Only the person a claim was sent to may give it."""
    try:
        claim = ExpenseClaim.objects.select_for_update().get(pk=claim_id, is_active=True)
    except ExpenseClaim.DoesNotExist:
        raise NotFound("No such expense.")
    if claim.approver_id != user.pk:
        raise PermissionDenied("This expense was not sent to you.")
    if claim.status != ExpenseClaimStatus.PENDING_APPROVAL:
        raise ValidationError({"detail": "This expense has already been decided."})

    note = (note or "").strip()
    if not approve and not note:
        raise ValidationError({"note": "Say why it is being rejected."})

    claim.status = ExpenseClaimStatus.APPROVED if approve else ExpenseClaimStatus.REJECTED
    claim.decided_at = timezone.now()
    claim.decided_by = user
    claim.decision_note = note
    claim.updated_by = user
    claim.save()

    transaction.on_commit(lambda: notifications.decided(claim, actor=user))
    return claim


def counts(queryset):
    """How many claims sit in each status."""
    found = dict(queryset.values("status").annotate(n=Count("id")).values_list("status", "n"))
    return {value: found.get(value, 0) for value in ExpenseClaimStatus.values}
