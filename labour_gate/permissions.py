# labour_gate/permissions.py
"""
Permission-based access control for the Labour Gate (in/out headcount) module.
"""
from rest_framework.permissions import BasePermission


class CanViewLabourGate(BasePermission):
    """Permission to view labour gate in/out entries."""

    def has_permission(self, request, view):
        return request.user.has_perm("labour_gate.view_labourgateentry")


class CanRecordLabourIn(BasePermission):
    """Permission to record how many labourers a contractor brought in."""

    def has_permission(self, request, view):
        return request.user.has_perm("labour_gate.can_record_labour_in")


class CanRecordLabourOut(BasePermission):
    """Permission to record labour leaving at the gate."""

    def has_permission(self, request, view):
        return request.user.has_perm("labour_gate.can_record_labour_out")
