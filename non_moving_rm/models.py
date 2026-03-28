"""
non_moving_rm/models.py

No database tables needed — all data is read live from SAP HANA.
This module exists solely to define custom permissions for the app.
"""

from django.db import models


class NonMovingRMPermission(models.Model):
    """
    Sentinel model that holds custom permissions for the Non-Moving RM Dashboard.
    No database rows are ever written to this table.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_non_moving_rm", "Can view Non-Moving RM Dashboard"),
        ]
