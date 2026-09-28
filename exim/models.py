from django.conf import settings
from django.db import models

from .access import PERMISSIONS


class EximPermission(models.Model):
    """Sentinel model carrying every EXIM permission (no table of its own).

    See ``exim.access`` for where the list comes from. This stays the one home
    of the module's rights as EXIM's screens move across: the models that arrive
    with them declare ``default_permissions = ()``, so a codename never exists
    twice under the ``exim`` label.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = PERMISSIONS


class EximUser(models.Model):
    """Which login here an EXIM account became.

    Written by ``import_exim_users``. EXIM's own tables record the EXIM user id
    of whoever made a change (a stock status edit session, for one), and ids
    differ between the two systems, so each module's data copy remaps those
    columns through this table. That is why it is kept after the import, and
    why a linked login cannot simply be deleted.
    """

    exim_id = models.BigIntegerField(unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="exim_accounts",
    )
    #: The address the account had in EXIM when it was last synced.
    exim_email = models.EmailField(max_length=255)
    #: True when the import created the login; False when EXIM's account was
    #: matched to one that already existed here. Only a created login follows
    #: EXIM's active flag.
    created_user = models.BooleanField()
    first_imported_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        ordering = ["exim_id"]

    def __str__(self) -> str:
        return f"EXIM #{self.exim_id} → {self.user_id}"
