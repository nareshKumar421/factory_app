"""
Two rights, matching the two groups the module ships with.

``can_view_artwork``   -- read the register, open and download the files.
``can_manage_artwork`` -- capture, revise and retire artwork.

Manage implies view at the read endpoints (see :class:`ArtworkPermission`),
because nobody should be able to file an artwork they cannot then look at.
"""

from rest_framework.permissions import BasePermission

#: HTTP verbs that change something.
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

VIEW_PERMISSION = "artwork.can_view_artwork"
MANAGE_PERMISSION = "artwork.can_manage_artwork"


class CanViewArtwork(BasePermission):
    """Read-only access. An editor passes this too."""

    message = "You do not have access to the artwork register."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return user.has_perm(VIEW_PERMISSION) or user.has_perm(MANAGE_PERMISSION)


class CanManageArtwork(BasePermission):
    """Capture, revise and retire."""

    message = "You may view artwork but not change it."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.has_perm(MANAGE_PERMISSION))


class ArtworkPermission(BasePermission):
    """One class for an endpoint that both reads and writes.

    Saves every view repeating the same ``get_permissions`` branch.
    """

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return CanManageArtwork().has_permission(request, view)
        return CanViewArtwork().has_permission(request, view)
