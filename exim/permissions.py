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


# ---------------------------------------------------------------------------
# The tank farm and oil lots. EXIM's codenames, unchanged: a lot is still
# "stockstatus", a tank "tankdata", an oil "tankitem", a shortage "debitentry".
# ---------------------------------------------------------------------------

class Rights:
    OIL_VIEW = "exim.view_tankitem"
    OIL_ADD = "exim.add_tankitem"
    OIL_CHANGE = "exim.change_tankitem"
    OIL_DELETE = "exim.delete_tankitem"

    TANK_VIEW = "exim.view_tankdata"
    TANK_ADD = "exim.add_tankdata"
    TANK_CHANGE = "exim.change_tankdata"
    TANK_DELETE = "exim.delete_tankdata"
    #: The weighted average cost of what is in the tanks.
    TANK_AVERAGE = "exim.view_itemwise_average"
    TANK_LOG_VIEW = "exim.view_tanklog"
    #: Put a tank's opening stock on as a lot. EXIM filed it under accounts.
    OPENING_STOCK = "exim.add_opening_rate"

    LOT_VIEW = "exim.view_stockstatus"
    LOT_ADD = "exim.add_stockstatus"
    LOT_CHANGE = "exim.change_stockstatus"
    LOT_DELETE = "exim.delete_stockstatus"
    VEHICLE_REPORT = "exim.view_vehicle_report"
    SHORTAGE_VIEW = "exim.view_debitentry"
    #: The change log of every lot at once (a lot's own history needs LOT_VIEW).
    CHANGE_LOG_VIEW = "exim.view_stockstatusupdatelog"
    CONTRACT_HISTORY_VIEW = "exim.view_contractualhistory"
    DASHBOARD_ORDER_CHANGE = "exim.change_dashboardorder"
    #: A vendor that is not in SAP yet. EXIM's vendors were "party".
    TEMP_VENDOR_ADD = "exim.add_party"
    DIRECTOR_REPORT = "exim.view_director_report"

    #: Oil contracts: EXIM's domestic contract register ("domesticreports")
    #: and its landed-cost sheet ("domesticcontractdetails"). Either opens the
    #: contracts; the terms a contract's landed cost adds need the change right.
    CONTRACT_VIEW = "exim.view_domesticreports"
    LANDED_COST_VIEW = "exim.view_domesticcontractdetails"
    CONTRACT_CHANGE = "exim.change_domesticreports"


def any_of(*rights):
    """A permission class passing a user who holds at least one of ``rights``."""

    class AnyOf(BasePermission):
        def has_permission(self, request, view):
            user = request.user
            return bool(user and any(user.has_perm(right) for right in rights))

    AnyOf.__name__ = "AnyOf(" + ", ".join(rights) + ")"
    return AnyOf
