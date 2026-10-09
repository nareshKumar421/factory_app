"""
Everything that moves an expense claim goes through here.
"""

import pathlib

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from company.models import Company
from sap_client.exceptions import SAPDataError

from . import notifications
from .constants import ATTACHMENT_EXTENSIONS, COMPANY_LABELS, MAX_ATTACHMENT_BYTES
from .hana_reader import ExpenseSapReader
from .models import ExpenseClaim, ExpenseClaimAttachment, ExpenseClaimStatus


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


def _checked(*, company, budget_code, gl_account_code, gl_description, comment):
    """Everything an expense is filed under, confirmed against the company's SAP.

    The G/L account is optional: somebody who does not know it says what the
    expense is for instead, and one of the two is required. Whatever is picked
    is confirmed against SAP and its name snapshotted. SAP being unreachable
    raises ``SAPConnectionError`` for the view to answer 503 -- the pickers
    this is fed from read SAP too, so trying again is the answer.
    """
    comment = (comment or "").strip()
    if not comment:
        raise ValidationError({"comment": "Say what the money was spent on."})
    code = (gl_account_code or "").strip()
    description = (gl_description or "").strip()
    if not code and not description:
        raise ValidationError(
            {"gl_account_code": "Pick the G/L account, or say what the expense is for."}
        )

    reader = ExpenseSapReader(company.code)
    try:
        budget = reader.budget(budget_code)
    except SAPDataError as exc:
        raise ValidationError({"budget_code": str(exc)}) from exc
    account = {"account_code": "", "account_name": ""}
    if code:
        try:
            account = reader.expense_account(code)
        except SAPDataError as exc:
            raise ValidationError({"gl_account_code": str(exc)}) from exc

    return {
        "company": company,
        "budget_code": budget["budget_code"],
        "budget_name": budget["budget_name"],
        "gl_account_code": account["account_code"],
        "gl_account_name": account["account_name"],
        # Kept only while no account is picked: once one is, it says it all.
        "gl_description": "" if code else description,
        "comment": comment,
    }


@transaction.atomic
def submit(*, user, company, budget_code, gl_account_code, gl_description, comment, amount):
    """Put an expense in. It goes to the expense approvers."""
    fields = _checked(
        company=company,
        budget_code=budget_code,
        gl_account_code=gl_account_code,
        gl_description=gl_description,
        comment=comment,
    )
    claim = ExpenseClaim.objects.create(
        **fields, amount=amount, created_by=user, updated_by=user
    )
    transaction.on_commit(lambda: notifications.waiting(claim, actor=user))
    return claim


def _own_unapproved(user, claim_id):
    """The caller's own expense, locked for the change, while it can still change."""
    try:
        claim = ExpenseClaim.objects.select_for_update().get(pk=claim_id, is_active=True)
    except ExpenseClaim.DoesNotExist:
        raise NotFound("No such expense.")
    if claim.created_by_id != user.pk:
        raise PermissionDenied("You can only change an expense you put in yourself.")
    if claim.status == ExpenseClaimStatus.APPROVED:
        raise ValidationError({"detail": "This expense is approved and can no longer be changed."})
    return claim


@transaction.atomic
def edit(
    *, user, claim_id, company, budget_code, gl_account_code, gl_description, comment, amount
):
    """Change an expense you put in, any time before it is approved.

    Editing a rejected one sends it again: it goes back to awaiting approval,
    the old verdict is cleared, and the approvers are told. A correction to
    one already waiting is not news to them.
    """
    claim = _own_unapproved(user, claim_id)

    fields = _checked(
        company=company,
        budget_code=budget_code,
        gl_account_code=gl_account_code,
        gl_description=gl_description,
        comment=comment,
    )
    was_rejected = claim.status == ExpenseClaimStatus.REJECTED

    for name, value in fields.items():
        setattr(claim, name, value)
    claim.amount = amount
    claim.status = ExpenseClaimStatus.PENDING_APPROVAL
    claim.decided_at = None
    claim.decided_by = None
    claim.decision_note = ""
    claim.updated_by = user
    claim.save()

    if was_rejected:
        transaction.on_commit(lambda: notifications.waiting(claim, actor=user))
    return claim


@transaction.atomic
def decide(*, user, claim_id, approve: bool, note=""):
    """An approver's verdict. Any approver may give it, but not on their own expense."""
    try:
        claim = ExpenseClaim.objects.select_for_update().get(pk=claim_id, is_active=True)
    except ExpenseClaim.DoesNotExist:
        raise NotFound("No such expense.")
    if claim.created_by_id == user.pk:
        raise PermissionDenied("Your own expense has to be approved by somebody else.")
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


def _check_upload(upload):
    """A photograph or a PDF, not empty and not too big. Returns its name and size."""
    name = getattr(upload, "name", "") or ""
    if pathlib.Path(name).suffix.lower() not in ATTACHMENT_EXTENSIONS:
        raise ValidationError(
            {
                "file": f"{name or 'That file'} is not a photograph or a PDF "
                f"({', '.join(sorted(ATTACHMENT_EXTENSIONS))})."
            }
        )
    size = getattr(upload, "size", 0) or 0
    if size <= 0:
        raise ValidationError({"file": f"{name} is empty."})
    if size > MAX_ATTACHMENT_BYTES:
        raise ValidationError(
            {
                "file": f"{name} is {size / 1024 / 1024:.1f} MB. The most that can "
                f"be attached is {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB."
            }
        )
    return name[:255], size


@transaction.atomic
def attach(*, user, claim_id, upload):
    """Put a bill on an expense you put in, any time before it is approved.

    It does not send a rejected expense again by itself: saving the form does.
    """
    claim = _own_unapproved(user, claim_id)
    name, size = _check_upload(upload)
    return ExpenseClaimAttachment.objects.create(
        claim=claim,
        file=upload,
        original_filename=name,
        size_bytes=size,
        created_by=user,
        updated_by=user,
    )


@transaction.atomic
def remove_attachment(*, user, attachment_id):
    """Take a file off an expense before it is approved, and off the disk with it."""
    try:
        attachment = ExpenseClaimAttachment.objects.get(pk=attachment_id)
    except ExpenseClaimAttachment.DoesNotExist:
        raise NotFound("No such attachment.")
    _own_unapproved(user, attachment.claim_id)
    attachment.delete()
    # Only once the row is surely gone: a rolled-back delete keeps its file.
    transaction.on_commit(lambda: attachment.file.delete(save=False))


def counts(queryset):
    """How many claims sit in each status."""
    found = dict(queryset.values("status").annotate(n=Count("id")).values_list("status", "n"))
    return {value: found.get(value, 0) for value in ExpenseClaimStatus.values}
