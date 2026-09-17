"""
universal_search/models.py

The module stores nothing. A search reads SAP and the app's own tables and
answers from them; there is no history worth keeping that the modules it
searches do not already keep themselves.

What does need a home is the permission, because Django hangs permissions off
models. Hence the sentinel below -- the same shape the stock dashboard uses.
"""

from django.db import models


class UniversalSearchPermission(models.Model):
    """Holds the module's permission. No table, no rows."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_use_universal_search", "Can use universal search"),
        ]
