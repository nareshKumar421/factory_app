"""Four rights, because four different people touch this module.

``can_view_fleet``             the manager who reads the costs.
``can_manage_fleet_vehicle``   whoever sets the fleet up. Rarely used.
``can_add_fleet_expense``      the clerk or driver entering fuel bills daily.
``can_approve_fleet_expense``  who passes a bill as real spend.

Entering and approving are deliberately separate rights: the point of the
approval step is that the person who files the bill is not the person who
passes it. Every right implies view -- nobody should be able to record a
filling they cannot then see.
"""

from rest_framework.permissions import BasePermission

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

VIEW_PERMISSION = "company_vehicle.can_view_fleet"
MANAGE_VEHICLE_PERMISSION = "company_vehicle.can_manage_fleet_vehicle"
ADD_EXPENSE_PERMISSION = "company_vehicle.can_add_fleet_expense"
APPROVE_EXPENSE_PERMISSION = "company_vehicle.can_approve_fleet_expense"

ALL_PERMISSIONS = (
    VIEW_PERMISSION,
    MANAGE_VEHICLE_PERMISSION,
    ADD_EXPENSE_PERMISSION,
    APPROVE_EXPENSE_PERMISSION,
)


def _has(user, *permissions) -> bool:
    if not user or not user.is_authenticated:
        return False
    return any(user.has_perm(permission) for permission in permissions)


class CanViewFleet(BasePermission):
    """Read the register. Anyone with any fleet right passes."""

    message = "You do not have access to the company vehicle register."

    def has_permission(self, request, view):
        return _has(request.user, *ALL_PERMISSIONS)


class CanManageFleetVehicle(BasePermission):
    message = "You may view the fleet but not add or edit vehicles."

    def has_permission(self, request, view):
        return _has(request.user, MANAGE_VEHICLE_PERMISSION)


class CanAddFleetExpense(BasePermission):
    message = "You may view the fleet but not record fuel or service entries."

    def has_permission(self, request, view):
        return _has(request.user, ADD_EXPENSE_PERMISSION, MANAGE_VEHICLE_PERMISSION)


class CanApproveFleetExpense(BasePermission):
    message = "You may not approve fuel or service entries."

    def has_permission(self, request, view):
        return _has(request.user, APPROVE_EXPENSE_PERMISSION)


class FleetVehiclePermission(BasePermission):
    """Read for viewers, write for vehicle managers."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return CanManageFleetVehicle().has_permission(request, view)
        return CanViewFleet().has_permission(request, view)


class FleetExpensePermission(BasePermission):
    """Read for viewers, write for whoever records entries."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return CanAddFleetExpense().has_permission(request, view)
        return CanViewFleet().has_permission(request, view)
