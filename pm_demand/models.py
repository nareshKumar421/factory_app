"""
pm_demand/models.py

No database tables needed -- every figure is read live from SAP HANA.
This module exists solely to define the module's custom permission.

``managed = False`` keeps Django from ever creating a table, and the app
deliberately ships no migrations folder: the permission row is created by the
``post_migrate`` signal, so nothing here has to be migrated against the live
database.
"""

from django.db import models


class PmDemandPermission(models.Model):
    """Sentinel model holding the PM Demand dashboard permission.

    No row is ever written to this table -- it does not exist.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_pm_demand", "Can view Packing Material Demand Dashboard"),
        ]
