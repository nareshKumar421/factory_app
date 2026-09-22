"""
URLs for the employee hierarchy module.

Order matters in one place: the master routes (``departments/``,
``designations/``, ``tree/`` …) are declared before ``employees/<int:...>/``
patterns that could otherwise swallow them. Everything is keyed by primary key
rather than employee code -- codes are re-used across companies and are edited
when somebody was entered wrong, and a URL that changes under you is worse than
one that is not pretty.
"""

from django.urls import path

from .views import (
    BranchDetailAPI,
    BranchListAPI,
    DepartmentDetailAPI,
    DepartmentListAPI,
    DesignationDetailAPI,
    DesignationListAPI,
    EmployeeAuditAPI,
    EmployeeDepartmentAPI,
    EmployeeDesignationAPI,
    EmployeeDetailAPI,
    EmployeeHistoryAPI,
    EmployeeListAPI,
    EmployeeManagerAPI,
    EmployeeMetaAPI,
    EmployeePromotionAPI,
    EmployeeReportingAPI,
    EmployeeSalaryAPI,
    EmployeeStatusAPI,
    OrgTreeAPI,
    PermanentLabourPresenceAPI,
    PermanentLabourPresenceAuditAPI,
    PermanentLabourStrengthAPI,
    PermanentLabourStrengthAuditAPI,
    SalaryApprovalQueueAPI,
    SalaryDecisionAPI,
    SalaryRevisionListAPI,
    WorkforceReportsAPI,
)

urlpatterns = [
    path("meta/", EmployeeMetaAPI.as_view(), name="employee-meta"),
    path("tree/", OrgTreeAPI.as_view(), name="employee-tree"),
    path("reports/", WorkforceReportsAPI.as_view(), name="employee-reports"),
    # Masters.
    path("departments/", DepartmentListAPI.as_view(), name="hr-department-list"),
    path(
        "departments/<int:department_id>/",
        DepartmentDetailAPI.as_view(),
        name="hr-department-detail",
    ),
    path("branches/", BranchListAPI.as_view(), name="hr-branch-list"),
    path("branches/<int:branch_id>/", BranchDetailAPI.as_view(), name="hr-branch-detail"),
    path("designations/", DesignationListAPI.as_view(), name="designation-list"),
    path(
        "designations/<int:designation_id>/",
        DesignationDetailAPI.as_view(),
        name="designation-detail",
    ),
    # Permanent labour: the strength on the rolls, and the daily presence
    # register measured against it.
    path("labour-strength/", PermanentLabourStrengthAPI.as_view(), name="labour-strength"),
    path("labour-presence/", PermanentLabourPresenceAPI.as_view(), name="labour-presence"),
    path(
        "labour-strength/audit/",
        PermanentLabourStrengthAuditAPI.as_view(),
        name="labour-strength-audit",
    ),
    path(
        "labour-presence/<int:presence_id>/audit/",
        PermanentLabourPresenceAuditAPI.as_view(),
        name="labour-presence-audit",
    ),
    # Salary, addressed by record rather than by employee.
    path("salary-approvals/", SalaryApprovalQueueAPI.as_view(), name="salary-approval-queue"),
    path("salary-revisions/", SalaryRevisionListAPI.as_view(), name="salary-revision-list"),
    path(
        "salary-records/<int:record_id>/approve/",
        SalaryDecisionAPI.as_view(),
        {"decision": "approve"},
        name="salary-approve",
    ),
    path(
        "salary-records/<int:record_id>/reject/",
        SalaryDecisionAPI.as_view(),
        {"decision": "reject"},
        name="salary-reject",
    ),
    # The people.
    path("employees/", EmployeeListAPI.as_view(), name="employee-list"),
    path("employees/<int:employee_id>/", EmployeeDetailAPI.as_view(), name="employee-detail"),
    path(
        "employees/<int:employee_id>/reporting/",
        EmployeeReportingAPI.as_view(),
        name="employee-reporting",
    ),
    path(
        "employees/<int:employee_id>/history/",
        EmployeeHistoryAPI.as_view(),
        name="employee-history",
    ),
    path("employees/<int:employee_id>/audit/", EmployeeAuditAPI.as_view(), name="employee-audit"),
    path(
        "employees/<int:employee_id>/manager/",
        EmployeeManagerAPI.as_view(),
        name="employee-manager",
    ),
    path(
        "employees/<int:employee_id>/department/",
        EmployeeDepartmentAPI.as_view(),
        name="employee-department",
    ),
    path(
        "employees/<int:employee_id>/designation/",
        EmployeeDesignationAPI.as_view(),
        name="employee-designation",
    ),
    path(
        "employees/<int:employee_id>/promote/",
        EmployeePromotionAPI.as_view(),
        name="employee-promote",
    ),
    path(
        "employees/<int:employee_id>/status/",
        EmployeeStatusAPI.as_view(),
        name="employee-status",
    ),
    path(
        "employees/<int:employee_id>/salary/",
        EmployeeSalaryAPI.as_view(),
        name="employee-salary",
    ),
]
