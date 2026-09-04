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


class CanManageWorkOrderAttachment(AnyDjangoPermission):
    """Whoever may raise a work order may attach its paperwork.

    `can_create_work_order` is in the list on purpose: the raise form uploads
    the staged files itself, so a raiser without the manage right would
    otherwise save the complaint and lose every attachment.
    """

    permissions = [
        "maintenance.can_manage_work_order",
        "maintenance.can_create_work_order",
        "maintenance.add_maintenanceworkorderattachment",
        "maintenance.change_maintenanceworkorderattachment",
        "maintenance.delete_maintenanceworkorderattachment",
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
    """Raise / edit / cancel an indent while it is still a draft.

    ``can_draft_material_indent`` is the draft-only right: it opens this
    endpoint but deliberately not :class:`CanSubmitMaterialIndent`, so a
    data-entry user parks the indent and somebody else sends it for approval.
    """

    permissions = [
        "maintenance.can_manage_material_indent",
        "maintenance.can_draft_material_indent",
        "maintenance.add_materialindent",
        "maintenance.change_materialindent",
    ]


class CanSubmitMaterialIndent(AnyDjangoPermission):
    """Send a draft indent for purchase approval.

    Split out of ``can_manage_material_indent``, which stays the legacy
    superset — everyone who could already submit still can. A holder of only
    ``can_draft_material_indent`` cannot.
    """

    permissions = [
        "maintenance.can_submit_material_indent",
        "maintenance.can_manage_material_indent",
    ]


class CanReviewMaterialIndent(DjangoPermission):
    """Store engineer: reviews a submitted indent, issues stock, forwards shortfall."""

    permission = "maintenance.can_review_material_indent"


class CanApproveMaterialIndent(DjangoPermission):
    """Higher authority: approves the purchase of shortfall items."""

    permission = "maintenance.can_approve_material_indent"


class CanPurchaseMaterialIndent(DjangoPermission):
    """Purchaser: handles approved-for-purchase indents."""

    permission = "maintenance.can_purchase_material_indent"


class CanGateInMaterialIndent(DjangoPermission):
    """Gate: records vehicle + invoice/bill when purchased goods arrive."""

    permission = "maintenance.can_gatein_material_indent"


class CanAttachMaterialIndent(AnyDjangoPermission):
    """Gate, store receiver, or manager may attach invoice/bill documents."""

    permissions = [
        "maintenance.can_gatein_material_indent",
        "maintenance.can_receive_material_indent",
        "maintenance.can_manage_material_indent",
        "maintenance.can_draft_material_indent",
    ]


class CanReceiveMaterialIndent(DjangoPermission):
    """Store: collects the arrival into Store/Spares stock."""

    permission = "maintenance.can_receive_material_indent"


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


# Daily Electricity is split per operation so a meter-master keeper, a daily
# data-entry operator and a read-only viewer can each get exactly their slice.
# ``can_manage_daily_electricity`` stays the legacy superset: anyone who holds it
# keeps every electricity right, so existing users/groups are unaffected.
_MANAGE_DAILY_ELECTRICITY = "maintenance.can_manage_daily_electricity"


class CanViewDailyElectricity(AnyDjangoPermission):
    """Read the register. Anyone who may act on it may also read it."""

    permissions = [
        "maintenance.can_view_daily_electricity",
        _MANAGE_DAILY_ELECTRICITY,
        "maintenance.can_add_daily_electricity",
        "maintenance.can_edit_daily_electricity",
        "maintenance.can_delete_daily_electricity",
        "maintenance.can_view_electricity_meter",
        "maintenance.can_manage_electricity_meter",
        "maintenance.view_electricitymeter",
        "maintenance.view_dailyelectricityreading",
    ]


class CanManageDailyElectricity(AnyDjangoPermission):
    permissions = [
        _MANAGE_DAILY_ELECTRICITY,
        "maintenance.add_dailyelectricityreading",
        "maintenance.change_dailyelectricityreading",
    ]


class CanViewElectricityMeter(AnyDjangoPermission):
    """Read the meter master — also needed by readers of the register, whose
    rows and prefills are meter-driven."""

    permissions = [
        "maintenance.can_view_electricity_meter",
        "maintenance.can_manage_electricity_meter",
        "maintenance.can_view_daily_electricity",
        _MANAGE_DAILY_ELECTRICITY,
        "maintenance.can_add_daily_electricity",
        "maintenance.can_edit_daily_electricity",
        "maintenance.can_delete_daily_electricity",
        "maintenance.view_electricitymeter",
    ]


class CanManageElectricityMeter(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_electricity_meter",
        _MANAGE_DAILY_ELECTRICITY,
        "maintenance.add_electricitymeter",
        "maintenance.change_electricitymeter",
    ]


class CanAddDailyElectricity(AnyDjangoPermission):
    permissions = [
        "maintenance.can_add_daily_electricity",
        _MANAGE_DAILY_ELECTRICITY,
        "maintenance.add_dailyelectricityreading",
    ]


class CanEditDailyElectricity(AnyDjangoPermission):
    permissions = [
        "maintenance.can_edit_daily_electricity",
        _MANAGE_DAILY_ELECTRICITY,
        "maintenance.change_dailyelectricityreading",
    ]


class CanDeleteDailyElectricity(AnyDjangoPermission):
    permissions = [
        "maintenance.can_delete_daily_electricity",
        _MANAGE_DAILY_ELECTRICITY,
        "maintenance.delete_dailyelectricityreading",
    ]


class CanViewDailyWastage(AnyDjangoPermission):
    permissions = [
        "maintenance.can_view_daily_wastage",
        "maintenance.can_manage_daily_wastage",
        "maintenance.view_dailywastagelog",
    ]


class CanManageDailyWastage(AnyDjangoPermission):
    permissions = [
        "maintenance.can_manage_daily_wastage",
        "maintenance.add_dailywastagelog",
        "maintenance.change_dailywastagelog",
    ]
