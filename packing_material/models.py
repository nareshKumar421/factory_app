"""
packing_material/models.py

No database tables needed -- every figure is read live from SAP HANA and from
FactoryFlow's existing gate-out register. This module exists solely to declare
the board's own permission.

``managed = False`` keeps Django from ever creating a table, and the app
deliberately ships no migrations folder: the permission row is created by the
``post_migrate`` signal, so nothing here has to be migrated against the live
database.
"""

from django.db import models


class PackingMaterialPermission(models.Model):
    """Sentinel model holding the packing-material board's permission.

    No row is ever written to this table -- it does not exist.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_packing_material", "Can view Packing Material Dashboard"),
        ]
