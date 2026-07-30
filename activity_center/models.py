from django.db import models


class ActivityCenter(models.Model):
    """
    Permission carrier for the Activity Center.

    The module stores no data of its own — every activity is derived live from the
    module that owns the record (see ``registry.py``). This model exists only so the
    three access permissions have a content type to hang off, which is why it is
    unmanaged and has its default add/change/delete/view permissions removed.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "Activity Center"
        verbose_name_plural = "Activity Center"
        permissions = [
            (
                "can_view_my_activities",
                "Can view own pending and completed activities",
            ),
            (
                "can_view_all_activities",
                "Can view every user's activities (supervisor)",
            ),
            (
                "can_view_activity_reports",
                "Can view activity completion reports",
            ),
        ]
