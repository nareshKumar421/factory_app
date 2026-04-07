"""
inventory_age/models.py

Proxy model used solely to register a custom permission for the
Inventory Age & Value dashboard.  No database table is created.
"""

from django.db import models


class InventoryAgePermission(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_inventory_age", "Can view Inventory Age & Value dashboard"),
        ]
