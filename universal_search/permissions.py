"""
universal_search/permissions.py

One gate on the modal itself. It is deliberately not the whole story: what
comes back is filtered a second time, per result, against the permission of
the module that owns it -- see ``services/app_lookup.py``. This right opens
the box; it does not decide what is in it.
"""

from rest_framework.permissions import BasePermission


class CanUseUniversalSearch(BasePermission):
    """Can open the search modal and run a lookup."""

    def has_permission(self, request, view):
        return request.user.has_perm("universal_search.can_use_universal_search")
