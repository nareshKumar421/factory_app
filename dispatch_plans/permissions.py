from rest_framework.permissions import BasePermission


def has_any_permission(user, *permissions):
    return any(user.has_perm(permission) for permission in permissions)


class CanViewDispatchPlans(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_view_dispatch_plans")


class CanViewDispatchPlansOrLinkDispatchVehicle(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm(
            "dispatch_plans.can_view_dispatch_plans"
        ) or request.user.has_perm("dispatch_plans.can_link_dispatch_vehicle")


class CanViewDispatchSchedule(BasePermission):
    """Read-only Dispatch Schedule (warehouse view). Anyone who can already view
    the dispatch plans dashboard also sees the schedule."""

    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_view_dispatch_schedule",
            "dispatch_plans.can_view_dispatch_plans",
        )


class CanViewDispatchPipeline(BasePermission):
    """Read-only Dispatch Pipeline (vehicle stage board). Anyone who can already
    view the dispatch plans dashboard also sees the pipeline."""

    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_view_dispatch_pipeline",
            "dispatch_plans.can_view_dispatch_plans",
        )


class CanSelectDispatchBills(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_select_dispatch_bills")


class CanLookupDispatchBill(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm(
            "dispatch_plans.can_view_dispatch_plans"
        ) or request.user.has_perm("person_gatein.can_view_dashboard")


class CanEditDispatchPlans(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_edit_dispatch_plans")


class CanEditDispatchPlansOrLinkDispatchVehicle(BasePermission):
    def has_permission(self, request, view):
        can_edit_dispatch_plans = request.user.has_perm(
            "dispatch_plans.can_view_dispatch_plans"
        ) and request.user.has_perm("dispatch_plans.can_edit_dispatch_plans")
        return can_edit_dispatch_plans or request.user.has_perm(
            "dispatch_plans.can_link_dispatch_vehicle"
        )


# --- Inside Vehicle Manager (dispatch correction console): one perm per action ---
class CanViewInsideVehicleManager(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_view_inside_vehicle_manager")


class CanAddBillInsideVehicle(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_add_bill_inside_vehicle")


class CanRemoveBillInsideVehicle(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_remove_bill_inside_vehicle")


class CanMoveBillInsideVehicle(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_move_bill_inside_vehicle")


class CanUnlinkBillsInsideVehicle(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_unlink_bills_inside_vehicle")


class CanViewOpenBilties(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_view_open_bilties")


class CanViewOpenBiltiesOrPostTransporterAPInvoice(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm(
            "dispatch_plans.can_view_open_bilties"
        ) or request.user.has_perm(
            "dispatch_plans.can_post_transporter_ap_invoice"
        )


class CanViewBiltyServiceGRPOQueue(BasePermission):
    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_post_bilty_service_grpo",
            "grpo.can_view_pending_grpo",
            "grpo.add_grpoposting",
        )


class CanPreviewBiltyServiceGRPO(BasePermission):
    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_post_bilty_service_grpo",
            "grpo.can_preview_grpo",
            "grpo.add_grpoposting",
        )


class CanPostBiltyServiceGRPO(BasePermission):
    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_post_bilty_service_grpo",
            "grpo.add_grpoposting",
        )


class CanViewBiltyServiceGRPOHistory(BasePermission):
    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_post_bilty_service_grpo",
            "grpo.can_view_grpo_history",
            "grpo.add_grpoposting",
        )


class CanViewBiltyServiceGRPODetail(BasePermission):
    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_post_bilty_service_grpo",
            "grpo.view_grpoposting",
            "grpo.add_grpoposting",
        )


class CanViewTransporterAPInvoice(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm(
            "dispatch_plans.can_view_transporter_ap_invoice"
        )


class CanPostTransporterAPInvoice(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm(
            "dispatch_plans.can_post_transporter_ap_invoice"
        )


# --- Bill summary (the picking sheet handed to the floor) -------------------
# Issuing and picking are separate permissions: the manager issues the sheet,
# the floor confirms what came off it, and one person doing both silently is
# exactly what the paper trail exists to prevent.

class CanViewBillSummary(BasePermission):
    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_view_bill_summary",
            "dispatch_plans.can_create_bill_summary",
            "dispatch_plans.can_pick_bill_summary",
        )


class CanCreateBillSummary(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_create_bill_summary")


class CanPickBillSummary(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_pick_bill_summary")


class CanCancelBillSummary(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_cancel_bill_summary")

