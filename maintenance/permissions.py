from rest_framework.permissions import BasePermission


class DjangoPermission(BasePermission):
    permission = ""

    def has_permission(self, request, view):
        return bool(request.user and request.user.has_perm(self.permission))


class AnyDjangoPermission(BasePermission):
    permissions = []

    def has_permission(self, request, view):
        return bool(
            request.user
            and any(request.user.has_perm(permission) for permission in self.permissions)
        )


class CanViewMaintenanceModule(DjangoPermission):
    permission = "maintenance.can_view_maintenance_module"


class CanViewMaintenanceDashboard(DjangoPermission):
    permission = "maintenance.can_view_maintenance_dashboard"


class CanViewMaintenanceReports(DjangoPermission):
    permission = "maintenance.can_view_maintenance_reports"


class CanManageMaintenanceSettings(DjangoPermission):
    permission = "maintenance.can_manage_maintenance_settings"


class CanViewAsset(DjangoPermission):
    permission = "maintenance.view_asset"


class CanGateMaintenanceLink(DjangoPermission):
    """Read access to maintenance reference data (assets / options / spares /
    work-orders) for gate "maintenance material-in" operators. They link these to
    a maintenance gate entry but hold only ``maintenance_gatein.*`` permissions.
    OR'd with the normal maintenance view permission at the relevant *read*
    endpoints only. Does NOT expose the Maintenance module (which gates on
    ``maintenance.*`` permissions)."""
    permission = "maintenance_gatein.add_maintenancegateentry"


class CanCreateAsset(DjangoPermission):
    permission = "maintenance.add_asset"


class CanEditAsset(DjangoPermission):
    permission = "maintenance.change_asset"


class CanDeleteAsset(DjangoPermission):
    permission = "maintenance.delete_asset"


class CanDeactivateAsset(DjangoPermission):
    permission = "maintenance.can_deactivate_asset"


class CanViewAssetAttachment(DjangoPermission):
    permission = "maintenance.view_assetdocument"


class CanManageAssetAttachment(DjangoPermission):
    def has_permission(self, request, view):
        if request.method == "POST":
            permissions = ["maintenance.add_assetphoto", "maintenance.add_assetdocument"]
        elif request.method == "DELETE":
            permissions = ["maintenance.delete_assetphoto", "maintenance.delete_assetdocument"]
        else:
            permissions = ["maintenance.change_assetphoto", "maintenance.change_assetdocument"]
        return bool(request.user and any(request.user.has_perm(permission) for permission in permissions))


class CanViewWorkOrder(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_work_order",
        "maintenance.view_maintenanceworkorder",
    ]


class CanCreateWorkOrder(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.can_create_work_order",
        "maintenance.add_maintenanceworkorder",
    ]


class CanManageWorkOrder(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.change_maintenanceworkorder",
    ]


class CanAssignWorkOrder(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.can_assign_work_order",
    ]


class CanStartWorkOrder(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.can_start_work_order",
    ]


class CanCompleteWorkOrder(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.can_complete_work_order",
    ]


class CanApproveWorkOrder(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.can_approve_work_order",
    ]


class CanCloseWorkOrder(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.can_close_work_order",
    ]


class CanManageWorkOrderPhoto(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.add_maintenanceworkorderphoto",
        "maintenance.change_maintenanceworkorderphoto",
        "maintenance.delete_maintenanceworkorderphoto",
    ]


class CanViewPM(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_pm",
        "maintenance.view_preventivemaintenanceplan",
        "maintenance.view_preventivemaintenanceexecution",
        "maintenance.view_maintenancechecklisttemplateitem",
        "maintenance.view_maintenancechecklistresult",
    ]


class CanManagePM(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_pm",
        "maintenance.add_preventivemaintenanceplan",
        "maintenance.change_preventivemaintenanceplan",
        "maintenance.add_preventivemaintenanceexecution",
        "maintenance.change_preventivemaintenanceexecution",
        "maintenance.add_maintenancechecklisttemplateitem",
        "maintenance.change_maintenancechecklisttemplateitem",
        "maintenance.add_maintenancechecklistresult",
        "maintenance.change_maintenancechecklistresult",
    ]


class CanViewSpare(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_spare",
        "maintenance.view_maintenancespare",
        "maintenance.view_sparerequest",
        "maintenance.view_sparemovement",
    ]


class CanManageSpare(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_spare",
        "maintenance.add_maintenancespare",
        "maintenance.change_maintenancespare",
        "maintenance.add_sparerequest",
        "maintenance.change_sparerequest",
    ]


class CanRequestSpare(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.can_create_work_order",
        "maintenance.can_manage_spare",
        "maintenance.add_sparerequest",
    ]


class CanViewFire(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_fire",
        "maintenance.view_maintenancefire",
        "maintenance.view_firerequest",
        "maintenance.view_firemovement",
    ]


class CanManageFire(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_fire",
        "maintenance.add_maintenancefire",
        "maintenance.change_maintenancefire",
        "maintenance.add_firerequest",
        "maintenance.change_firerequest",
    ]


class CanRequestFire(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.can_create_work_order",
        "maintenance.can_manage_fire",
        "maintenance.add_firerequest",
    ]


class CanViewFireReport(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_fire_report",
        "maintenance.view_fireshiftreport",
        "maintenance.view_fireshiftreportitem",
    ]


class CanManageFireReport(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_fire_report",
        "maintenance.add_fireshiftreport",
        "maintenance.change_fireshiftreport",
    ]


class CanReviewFireReport(AnyDjangoPermission):
    permissions = [
        "maintenance.can_review_fire_report",
        "maintenance.can_manage_fire_report",
    ]


class CanViewFireIssue(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_fire_issue",
        "maintenance.view_fireequipmentissue",
        "maintenance.view_fireequipmentissueitem",
    ]


class CanManageFireIssue(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_fire_issue",
        "maintenance.add_fireequipmentissue",
        "maintenance.change_fireequipmentissue",
    ]


class CanViewSafetyFine(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_safety_fine",
        "maintenance.view_safetyfine",
    ]


class CanManageSafetyFine(DjangoPermission):
    """Issuing/settling a fine is restricted to the Fire Department Head.

    Deliberately requires the custom permission only — the Django model
    permissions (add/change_safetyfine) must NOT grant it, otherwise a view-only
    group could create fines through the API while the UI button stays hidden.
    """

    permission = "maintenance.can_manage_safety_fine"


class CanViewMaterialIndent(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_material_indent",
        "maintenance.view_materialindent",
    ]


class CanManageMaterialIndent(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_material_indent",
        "maintenance.add_materialindent",
        "maintenance.change_materialindent",
    ]


class CanApproveMaterialIndent(DjangoPermission):
    """Approving an indent is restricted to the dedicated higher-authority permission."""

    permission = "maintenance.can_approve_material_indent"


class CanViewWorkPermit(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_work_permit",
        "maintenance.view_workpermit",
    ]


class CanManageWorkPermit(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_work_permit",
        "maintenance.add_workpermit",
        "maintenance.change_workpermit",
    ]


class CanIssueWorkPermit(AnyDjangoPermission):
    permissions = [
        "maintenance.can_issue_work_permit",
        "maintenance.can_manage_work_permit",
    ]


class CanApproveWorkPermit(AnyDjangoPermission):
    permissions = [
        "maintenance.can_approve_work_permit",
        "maintenance.can_manage_work_permit",
    ]


class CanAcceptWorkPermit(AnyDjangoPermission):
    permissions = [
        "maintenance.can_accept_work_permit",
        "maintenance.can_manage_work_permit",
    ]


class CanCloseWorkPermit(AnyDjangoPermission):
    permissions = [
        "maintenance.can_close_work_permit",
        "maintenance.can_manage_work_permit",
    ]


class CanViewVendor(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_vendor",
        "maintenance.view_maintenancevendorvisit",
    ]


class CanManageVendor(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_vendor",
        "maintenance.add_maintenancevendorvisit",
        "maintenance.change_maintenancevendorvisit",
        "maintenance.delete_maintenancevendorvisit",
    ]
