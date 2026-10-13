# fixed_asset_grpo/permissions.py
"""Who may see, link and post fixed-asset GRPOs.

Three rights, all declared on ``FixedAssetGRPORecord.Meta`` and all handed out
by the "Fixed Asset GRPO" group (migration 0002):

| Right | Lets a person |
|---|---|
| ``can_view_fixed_asset_grpo`` | see the lists, an entry, the preview and history |
| ``can_link_fixed_asset_po`` | link, change or remove the SAP PO on an FA entry |
| ``can_post_fixed_asset_grpo`` | post (or simulate), close / reopen, discard; implies the other two |

None of grpo's own rights opens anything here. ``grpo.add_grpoposting`` in
particular does **not** post a fixed-asset GRPO from these endpoints. (grpo's
generic ``/grpo/draft/`` endpoint still can, given the ids: the known residual
in docs/README.md, which needs an edit to grpo to close.)
"""

from rest_framework.permissions import BasePermission

from .constants import LINK_PERMISSION, POST_PERMISSION, VIEW_PERMISSION


def _has_any(user, *perms) -> bool:
    return any(user.has_perm(perm) for perm in perms)


def can_view(user) -> bool:
    return _has_any(user, VIEW_PERMISSION, POST_PERMISSION, LINK_PERMISSION)


def can_link(user) -> bool:
    return _has_any(user, LINK_PERMISSION, POST_PERMISSION)


def can_post(user) -> bool:
    return user.has_perm(POST_PERMISSION)


class CanViewFixedAssetGRPO(BasePermission):
    """Any of the three rights: someone who links or posts also has to see."""

    message = "You do not have permission to view fixed asset GRPOs."

    def has_permission(self, request, view):
        return can_view(request.user)


class CanLinkFixedAssetPO(BasePermission):
    """Link, change or remove the SAP PO on an FA gate entry (link or post right)."""

    message = "You do not have permission to link a PO to a fixed asset gate entry."

    def has_permission(self, request, view):
        return can_link(request.user)


class CanPostFixedAssetGRPO(BasePermission):
    """Post, close, reopen and discard: the post right only."""

    message = "You do not have permission to post fixed asset GRPOs."

    def has_permission(self, request, view):
        return can_post(request.user)
