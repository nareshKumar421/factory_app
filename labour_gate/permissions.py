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


class CanAllocateLabourDepartment(BasePermission):
    """Permission for an HOD to split a contractor's gate labour across departments."""

    def has_permission(self, request, view):
        return request.user.has_perm("labour_gate.can_allocate_labour_department")


class CanRecordOrAllocateLabourIn(BasePermission):
    """Routes the shared Labour-In write endpoint by intent (separation of concerns):

    - A raw gate-in (no ``department`` in the body) is the gate person's job and
      requires ``labour_gate.can_record_labour_in``.
    - A per-department split (``department`` present) is the HOD's job and requires
      ``labour_gate.can_allocate_labour_department``.
    """

    def has_permission(self, request, view):
        if request.data.get("department"):
            return request.user.has_perm("labour_gate.can_allocate_labour_department")
        return request.user.has_perm("labour_gate.can_record_labour_in")


class CanManageLabourEntry(BasePermission):
    """Per-entry edit/delete/restore, routed by the entry's own kind.

    A gate-in row (no department) is managed by the gate person
    (``can_record_labour_in``); a department-allocation row is managed by the HOD
    (``can_allocate_labour_department``). The object-level check must be invoked
    explicitly by the view via ``self.check_object_permissions(request, entry)``.
    """

    def has_permission(self, request, view):
        # Let the request through to the object-level check if the user holds
        # either labour role; the real decision is made in has_object_permission.
        return request.user.has_perm(
            "labour_gate.can_record_labour_in"
        ) or request.user.has_perm("labour_gate.can_allocate_labour_department")

    def has_object_permission(self, request, view, obj):
        if obj.department_id:
            return request.user.has_perm("labour_gate.can_allocate_labour_department")
        return request.user.has_perm("labour_gate.can_record_labour_in")
