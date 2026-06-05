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
