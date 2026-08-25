"""
DRF permission classes for the ETP / STP module.

One pair per register (view / manage) plus a settings right for the masters, so
a plant operator can hold the daily log without the calibration record and
nobody edits the masters by accident. ``can_manage_*`` implies the matching
``can_view_*`` at the read endpoints — see :class:`RegisterPermission`.
"""

from rest_framework.permissions import BasePermission

#: HTTP verbs that write.
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class EtpPermission(BasePermission):
    """Base: a single Django permission string."""

    permission = ""

    def has_permission(self, request, view):
        return bool(request.user and request.user.has_perm(self.permission))


class AnyEtpPermission(BasePermission):
    """Base: any one of several Django permissions is enough."""

    permissions: list = []

    def has_permission(self, request, view):
        return bool(
            request.user
            and any(request.user.has_perm(perm) for perm in self.permissions)
        )


class CanViewEtpModule(EtpPermission):
    permission = "etp.can_view_etp_module"


class CanManageEtpSettings(EtpPermission):
    permission = "etp.can_manage_etp_settings"


class RegisterPermission(BasePermission):
    """Read needs ``view_permission`` OR ``manage_permission``; writes need manage.

    Subclasses set the two strings. Reading is deliberately open to whoever can
    write — nobody should be able to file an entry they cannot then look at.
    """

    view_permission = ""
    manage_permission = ""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if request.method in WRITE_METHODS:
            return user.has_perm(self.manage_permission)
        return user.has_perm(self.view_permission) or user.has_perm(
            self.manage_permission
        )


class DailyLogPermission(RegisterPermission):
    view_permission = "etp.can_view_etp_daily_log"
    manage_permission = "etp.can_manage_etp_daily_log"


class MonitoringPermission(RegisterPermission):
    view_permission = "etp.can_view_etp_monitoring"
    manage_permission = "etp.can_manage_etp_monitoring"


class CanVerifyMonitoring(EtpPermission):
    permission = "etp.can_verify_etp_monitoring"


class ChemicalPermission(RegisterPermission):
    view_permission = "etp.can_view_etp_chemical"
    manage_permission = "etp.can_manage_etp_chemical"


class SludgePermission(RegisterPermission):
    view_permission = "etp.can_view_etp_sludge"
    manage_permission = "etp.can_manage_etp_sludge"


class BackwashPermission(RegisterPermission):
    view_permission = "etp.can_view_etp_backwash"
    manage_permission = "etp.can_manage_etp_backwash"


class CalibrationPermission(RegisterPermission):
    view_permission = "etp.can_view_etp_calibration"
    manage_permission = "etp.can_manage_etp_calibration"


class MasterPermission(BasePermission):
    """Masters: anyone on the module may read them, only settings may write.

    Every register form needs the plant / chemical / staff lists to render, so
    read access is granted to any holder of a module permission rather than to
    the settings right alone.
    """

    #: Any of these is enough to READ a master list.
    READ_PERMISSIONS = (
        "etp.can_view_etp_module",
        "etp.can_view_etp_daily_log",
        "etp.can_manage_etp_daily_log",
        "etp.can_view_etp_monitoring",
        "etp.can_manage_etp_monitoring",
        "etp.can_view_etp_chemical",
        "etp.can_manage_etp_chemical",
        "etp.can_view_etp_sludge",
        "etp.can_manage_etp_sludge",
        "etp.can_view_etp_backwash",
        "etp.can_manage_etp_backwash",
        "etp.can_view_etp_calibration",
        "etp.can_manage_etp_calibration",
        "etp.can_manage_etp_settings",
    )

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if request.method in WRITE_METHODS:
            return user.has_perm("etp.can_manage_etp_settings")
        return any(user.has_perm(perm) for perm in self.READ_PERMISSIONS)
