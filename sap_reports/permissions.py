"""
sap_reports/permissions.py

Two levels of access. Viewing lets a user run the reports they are given;
managing lets an admin sync the catalogue from SAP, rename reports, correct
parameter labels and read the underlying SQL.
"""

from rest_framework.permissions import BasePermission


class CanViewSapReports(BasePermission):
    """Can list and run the company's SAP reports."""

    def has_permission(self, request, view):
        return request.user.has_perm("sap_reports.can_view_sap_reports")


class CanManageSapReports(BasePermission):
    """Can sync from SAP, edit a report's setup, and see its SQL."""

    def has_permission(self, request, view):
        return request.user.has_perm("sap_reports.can_manage_sap_reports")
