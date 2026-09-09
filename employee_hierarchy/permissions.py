"""
DRF permission classes for the module.

The directory and the money are two different doors, and these classes are
where that separation starts. Reading the org chart needs
``can_view_employees``; the salary endpoints need one of the four salary
grants, and even then :mod:`employee_hierarchy.access` decides *whose* figures
come back. A user with every directory right and no salary right can browse the
whole company and never see a rupee.

Writing is split further, because proposing and approving are different jobs:
``can_create_salary`` / ``can_update_salary`` draft a revision, and only
``can_approve_salary_revision`` puts one in force.
"""

from rest_framework.permissions import BasePermission

from .access import (
    ANY_ACCESS,
    ANY_SALARY_ACCESS,
    APPROVE_SALARY,
    CREATE_SALARY,
    MANAGE_EMPLOYEES,
    MANAGE_STRUCTURE,
    UPDATE_SALARY,
    VIEW_AUDIT,
    VIEW_REPORTS,
    has_any,
)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class CanViewEmployees(BasePermission):
    """Read the directory and the chart; writing needs the manage right."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return has_any(request.user, (MANAGE_EMPLOYEES,))
        return has_any(request.user, ANY_ACCESS)


class CanManageEmployees(BasePermission):
    """Endpoints that only ever change people or the tree."""

    def has_permission(self, request, view):
        return has_any(request.user, (MANAGE_EMPLOYEES,))


class CanManageStructure(BasePermission):
    """The department and designation masters: read widely, edit narrowly."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return has_any(request.user, (MANAGE_STRUCTURE,))
        return has_any(request.user, ANY_ACCESS)


class CanViewWorkforceReports(BasePermission):
    """Headcount and org reports. Salary figures inside them are gated again."""

    def has_permission(self, request, view):
        return has_any(request.user, (VIEW_REPORTS, MANAGE_EMPLOYEES))


class CanViewEmployeeAudit(BasePermission):
    """The audit trail -- who changed what. Narrow on purpose."""

    def has_permission(self, request, view):
        return has_any(request.user, (VIEW_AUDIT,))


class CanAccessSalary(BasePermission):
    """The salary endpoints.

    Getting through this class means "you may see *some* salary"; it never
    means you may see *this* salary. That question belongs to
    :class:`employee_hierarchy.access.SalaryReach`, which every salary view
    consults per employee -- so an employee with only ``can_view_own_salary``
    reaches the endpoint and is answered about themselves alone.
    """

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return has_any(request.user, (CREATE_SALARY, UPDATE_SALARY, APPROVE_SALARY))
        return has_any(request.user, ANY_SALARY_ACCESS)


class CanApproveSalary(BasePermission):
    """Putting a revision in force."""

    def has_permission(self, request, view):
        return has_any(request.user, (APPROVE_SALARY,))
