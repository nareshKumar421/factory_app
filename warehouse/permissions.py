"""
Permission classes for the Warehouse module (BOM requests + FG receipts).

Dedicated `warehouse.*` permissions so warehouse-store users can be granted
BOM/FG access without borrowing production_execution permissions (which would
also expose the whole production module).
"""

from rest_framework.permissions import BasePermission


class CanViewBOMRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_bom_request")


class CanCreateBOMRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_create_bom_request")


class CanApproveBOMRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_approve_bom_request")


class CanIssueMaterials(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_issue_materials")


class CanViewFGReceipt(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_fg_receipt")


class CanCreateFGReceipt(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_create_fg_receipt")


class CanReceiveFG(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_receive_fg")


class CanPostFGToSAP(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_post_fg_to_sap")


# --- Warehouse transfer requests -------------------------------------------
# Approval is separated from raising deliberately: the point of the flow is that
# the receiving warehouse decides, so the two must be grantable independently.

class CanViewTransferRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_transfer_request")


class CanCreateTransferRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_create_transfer_request")


class CanApproveTransferRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_approve_transfer_request")


class CanPostTransferToSAP(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_post_transfer_to_sap")


# --- Warehouse managers (per-user warehouse scoping) ------------------------
# Separate from the movement permissions on purpose: deciding WHO runs a
# warehouse is an administrator's job, not something a warehouse manager should
# be able to grant themselves.

class CanManageUserWarehouses(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_manage_user_warehouses")


# --- Raw-material stock register -------------------------------------------
# Viewing is separate from setting because the register is read by production
# planning and supervisors, while only the store keeper of a warehouse states
# what is on its floor. Holding `can_set_rm_stock` is necessary but not
# sufficient: the service also insists the user manages that warehouse (see
# services/warehouse_scope).

class CanViewRMStock(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_rm_stock")


class CanSetRMStock(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_set_rm_stock")


# --- Barcode receiving (godown gate) ---------------------------------------
# Deliberately a warehouse permission, not a barcode one: the person who stands
# at the godown gate is warehouse staff, and the real restriction is the
# UserWarehouse assignment enforced alongside it (see services/warehouse_scope).

class CanReceiveBarcodes(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_receive_barcodes")


# --- Godown outward-movement register --------------------------------------
# Same split, same reason as the raw-material register above: the dashboard
# this feeds is read widely, while only the keeper of a floor declares what is
# leaving it. `can_record_pf_movement` is necessary but not sufficient — the
# service also insists the user manages the source warehouse.

class CanViewPFMovement(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_pf_movement")


class CanRecordPFMovement(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_record_pf_movement")


# --- SAP credit-note approvals ---------------------------------------------
# SAP's own approval queue on A/R and A/P credit-note drafts. Separate from the
# transfer permissions because they are a different queue for different people:
# a credit note is a finance document, and half of them (the service ones) never
# touch a warehouse at all.
#
# Scoped per FAMILY. A/R credit notes credit a customer (sales) and A/P ones
# debit a vendor (purchasing), and in SAP the two queues' authorizers do not
# overlap by a single account. So the permissions do not either: holding the
# A/R pair says nothing about A/P, and `visible_families` / `approvable_families`
# below are what the views filter and gate on rather than a blanket yes/no.
#
# None of it is sufficient on its own — SAP still accepts a decision only from
# the one authorizer it named on the request's current stage (see
# views_sap_approval_base).

FAMILY_AR = "AR"
FAMILY_AP = "AP"

# family -> (view permission, approve permission)
CREDIT_NOTE_FAMILY_PERMS = {
    FAMILY_AR: (
        "warehouse.can_view_ar_credit_note_approval",
        "warehouse.can_approve_ar_credit_note",
    ),
    FAMILY_AP: (
        "warehouse.can_view_ap_credit_note_approval",
        "warehouse.can_approve_ap_credit_note",
    ),
}


def visible_credit_note_families(user) -> set:
    """The families this user may read. Empty means the page is closed to them."""
    return {
        family
        for family, (view_perm, _) in CREDIT_NOTE_FAMILY_PERMS.items()
        if user.has_perm(view_perm)
    }


def approvable_credit_note_families(user) -> set:
    """The families this user may decide. Approving also requires the view perm.

    Requiring both keeps a nonsensical grant (approve without view) from
    producing a row the user can act on but never see listed.
    """
    return {
        family
        for family, (view_perm, approve_perm) in CREDIT_NOTE_FAMILY_PERMS.items()
        if user.has_perm(view_perm) and user.has_perm(approve_perm)
    }


class CanViewCreditNoteApproval(BasePermission):
    """Either family's view permission opens the page; the queue is then filtered."""

    def has_permission(self, request, view):
        return bool(visible_credit_note_families(request.user))


class CanApproveCreditNote(BasePermission):
    """Gate on the endpoint. WHICH family is then checked against the document."""

    def has_permission(self, request, view):
        return bool(approvable_credit_note_families(request.user))


class CanPrintARCreditNote(BasePermission):
    """The printed credit note is a sales document, so it needs the A/R grant.

    Not ``CanViewCreditNoteApproval``: that opens on either family, and a
    vendor-only approver holding it would otherwise reach a sheet made of a
    customer's name, address and GST number.
    """

    def has_permission(self, request, view):
        return FAMILY_AR in visible_credit_note_families(request.user)
