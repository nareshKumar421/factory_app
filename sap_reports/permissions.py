"""
sap_reports/permissions.py

Three levels of access. Viewing lets a user run the reports they are given;
syncing lets them also pull the catalogue in from SAP; managing lets an admin
do that and rename reports, correct parameter labels, read the underlying SQL
and see every report.

Sync is its own right because manage lifts the per-user report scoping (see
``services.access``): handing out manage just so somebody can press "Sync from
SAP" would show them every report in the company.
"""

from rest_framework.permissions import BasePermission


class CanViewSapReports(BasePermission):
    """Can list and run the company's SAP reports."""

    def has_permission(self, request, view):
        return request.user.has_perm("sap_reports.can_view_sap_reports")


class CanSyncSapReports(BasePermission):
    """Can pull the catalogue in from SAP. Implied by manage."""

    def has_permission(self, request, view):
        return request.user.has_perm("sap_reports.can_sync_sap_reports") or request.user.has_perm(
            "sap_reports.can_manage_sap_reports"
        )


class CanManageSapReports(BasePermission):
    """Can sync from SAP, edit a report's setup, and see its SQL."""

    def has_permission(self, request, view):
        return request.user.has_perm("sap_reports.can_manage_sap_reports")
