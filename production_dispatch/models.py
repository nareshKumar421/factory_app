"""
production_dispatch/models.py

The page keeps nothing of its own -- every figure is read from SAP when it is
opened -- so the only model is the sentinel that carries its right.
"""

from django.db import models


class ProductionDispatchPermission(models.Model):
    """Sentinel model carrying the page's right (no table of its own)."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            (
                "can_view_production_dispatch",
                "Can view the Production & Dispatch (pallet) report",
            ),
        ]
