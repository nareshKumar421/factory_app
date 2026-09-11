"""
Every new account is an issue reporter.

Being able to say "this screen is broken" is not a privilege, so the
**Issue Reporter** group is attached to an account the moment it is created
instead of being remembered by hand in the admin. Accounts that already exist
are backfilled once with ``manage.py setup_issue_groups --assign-everyone``.

A missing group is deliberately ignored rather than raised: a fresh database
creates accounts (the first superuser, test fixtures) before the seeding
command has ever run, and creating a user must not fail because the issue
tracker has not been set up yet.
"""

from django.conf import settings
from django.contrib.auth.models import Group
from django.db.models.signals import post_save
from django.dispatch import receiver

from .constants import REPORTER_GROUP


@receiver(
    post_save,
    sender=settings.AUTH_USER_MODEL,
    dispatch_uid="issues.assign_reporter_group",
)
def assign_reporter_group(sender, instance, created, raw=False, **kwargs):
    """Put a newly created user in the reporter group."""
    if not created or raw:
        return

    group = Group.objects.filter(name=REPORTER_GROUP).first()
    if group is not None:
        instance.groups.add(group)
