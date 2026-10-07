"""
Every new account may put in an expense.

Spending money for the factory is not a privilege, so the **Expense
Submitter** group is attached to an account the moment it is created -- the
same arrangement as the issue tracker's reporter group. Accounts that already
exist are backfilled once with
``manage.py setup_expense_claim_groups --assign-everyone``.

A missing group is ignored rather than raised: a fresh database creates
accounts before the setup command has ever run.
"""

from django.conf import settings
from django.contrib.auth.models import Group
from django.db.models.signals import post_save
from django.dispatch import receiver

from .constants import SUBMITTER_GROUP


@receiver(
    post_save,
    sender=settings.AUTH_USER_MODEL,
    dispatch_uid="expense_claims.assign_submitter_group",
)
def assign_submitter_group(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return

    group = Group.objects.filter(name=SUBMITTER_GROUP).first()
    if group is not None:
        instance.groups.add(group)
