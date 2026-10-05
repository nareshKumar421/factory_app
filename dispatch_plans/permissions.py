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
    """A goods-return clerk books returns against dispatched invoices, so being
    allowed to create a return implies being allowed to look one up."""

    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_view_dispatch_plans",
            "person_gatein.can_view_dashboard",
            "goods_return.can_create_goods_return",
        )


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


# --- Bill summary (the picking sheet dispatch raises and the warehouse dates) -
# Raising, approving and picking are three permissions because they are three
# desks. Dispatch fills the sheet in; the warehouse gives it a dispatch date,
# which is the moment SAP is written to; the floor confirms what came off it.
# One person holding all three can date and pick a dispatch nobody checked,
# which is exactly what the paper trail exists to prevent — so anyone who
# genuinely needs more than one gets them deliberately and visibly.

class CanViewBillSummary(BasePermission):
    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_view_bill_summary",
            "dispatch_plans.can_create_bill_summary",
            "dispatch_plans.can_approve_bill_summary",
            "dispatch_plans.can_pick_bill_summary",
        )


class CanPrintInvoice(BasePermission):
    """SAP's own TAX INVOICE for one bill — the bill, not the picking sheet.

    Held by the bill-summary desk, and also by the dispatch planners: the Plan
    page lists these very bills with their party, value and litres, so handing a
    planner the bill itself discloses nothing the board has not already shown
    them. ``CanViewBillSummary`` is deliberately left alone rather than widened —
    that one opens the picking-sheet queue, which is a different screen and a
    different job.
    """

    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_view_bill_summary",
            "dispatch_plans.can_create_bill_summary",
            "dispatch_plans.can_pick_bill_summary",
            "dispatch_plans.can_view_dispatch_plans",
        )


class CanCreateBillSummary(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_create_bill_summary")


class CanApproveBillSummary(BasePermission):
    """The warehouse desk: sets the dispatch date, or sends the sheet back.

    Deliberately NOT implied by ``can_create_bill_summary``. The dispatch date is
    the whole of what approval decides and the only thing SAP is really being
    told; letting the desk that raised the sheet also date it puts the flow back
    where it started.
    """

    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_approve_bill_summary")


class CanReconcileBillSummaryWithSap(BasePermission):
    """Retry the SAP posting for a sheet SAP refused.

    Both desks, because either could be the one looking at the failure: the
    warehouse's approval is what triggered the posting in the first place, and
    the dispatch desk is who cancels a sheet — which is the other thing this
    retries. Neither can decide anything with it; it only makes SAP agree with a
    decision already recorded here.
    """

    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_create_bill_summary",
            "dispatch_plans.can_approve_bill_summary",
        )


class CanPickBillSummary(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_pick_bill_summary")


class CanCancelBillSummary(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_cancel_bill_summary")



class CanViewDispatchSheet(BasePermission):
    """The Dispatch Sheet — the outward register, read-only.

    ONE permission, on purpose, and no relatives.

    It used to open for anyone holding ``can_view_dispatch_plans`` or
    ``can_view_dispatch_schedule`` too, on the reasoning that they were looking
    at the same rows arranged differently. On the live books that let
    twenty-four people read it, most of them through a dashboard group with no
    dispatch role at all — an audience nobody had chosen. The register carries
    every customer, every address and what each load cost to move, so who reads
    it is a decision somebody should make rather than one that follows from
    another right.
    """

    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_view_dispatch_sheet")


class CanViewFreightBenchmarks(BasePermission):
    """The benchmark freight table. Whoever may edit it may read it, and so may
    the linking desk: Vehicle Linking picks each truck's destination from this
    list and shows the benchmark beside the freight. Reading it is not the
    Freight Benchmarks page, which keeps its own rights."""

    def has_permission(self, request, view):
        return has_any_permission(
            request.user,
            "dispatch_plans.can_view_freight_benchmarks",
            "dispatch_plans.can_manage_freight_benchmarks",
            "dispatch_plans.can_link_dispatch_vehicle",
        )


class CanManageFreightBenchmarks(BasePermission):
    """Editing the benchmarks is a right of its own: the vehicle-linking
    approval holds an actual freight against them, so whoever can move a
    benchmark can move what counts as over it."""

    def has_permission(self, request, view):
        return request.user.has_perm("dispatch_plans.can_manage_freight_benchmarks")
