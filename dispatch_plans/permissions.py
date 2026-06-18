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

