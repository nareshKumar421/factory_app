"""Which ``exim.*`` right each thing in this module needs.

EXIM's rights are kept 1:1 (see ``exim.access``), and EXIM gated its licences
per table: an Advance header, its import lines and its export lines each have
their own view/add/change/delete, and DFIA the same. Here the two kinds share
one model (``exim.models_licence``), so the right a request needs depends on the
licence's kind and, for a line, its direction. ``licence_right`` is the one
place that mapping lives; the views call ``require`` with it.
"""

from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from .models_licence import LicenceKind, LineDirection

# EXIM's model name, as it appears in the codename, for each part of a licence.
_LICENCE_MODELS = {
    (LicenceKind.ADVANCE, None): "advancelicenseheaders",
    (LicenceKind.ADVANCE, LineDirection.IMPORT): "advancelicenseimportlines",
    (LicenceKind.ADVANCE, LineDirection.EXPORT): "advancelicenseexportlines",
    (LicenceKind.DFIA, None): "dfialicenseheader",
    (LicenceKind.DFIA, LineDirection.IMPORT): "dfialicenseimportlines",
    (LicenceKind.DFIA, LineDirection.EXPORT): "dfialicenseexportlines",
}

ACTIONS = ("view", "add", "change", "delete")


def licence_right(action: str, kind: str, direction: str | None = None) -> str:
    """The right to ``action`` a licence of ``kind`` (a header when ``direction``
    is None, else its lines of that direction), e.g. ``exim.add_dfialicenseimportlines``."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}")
    return f"exim.{action}_{_LICENCE_MODELS[(kind, direction)]}"


def require(user, right: str) -> None:
    """Refuse the request unless ``user`` holds ``right``."""
    if not user.has_perm(right):
        raise PermissionDenied("You do not have permission to do that.")


class DjangoPermission(BasePermission):
    permission = ""

    def has_permission(self, request, view):
        return bool(request.user and request.user.has_perm(self.permission))


class CanViewCustomsRates(DjangoPermission):
    #: EXIM filed this one under its accounts app; the codename is unchanged.
    permission = "exim.view_exim_rates"
