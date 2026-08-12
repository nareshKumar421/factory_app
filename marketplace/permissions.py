"""Permission classes for the marketplace app.

Custom permission codenames are declared in the model ``Meta.permissions`` blocks
(see ``models.py``) and bundled into a ``marketplace`` group by the data migration.
"""
from rest_framework.permissions import BasePermission


def _perm(codename):
    class _HasPerm(BasePermission):
        message = f"You do not have the '{codename}' permission."

        def has_permission(self, request, view):
            return bool(request.user and request.user.has_perm(codename))

    _HasPerm.__name__ = f"Has_{codename.replace('.', '_')}"
    return _HasPerm


CanViewDispatch = _perm("marketplace.view_dispatch")
CanAddDispatch = _perm("marketplace.add_dispatch")
CanScanDispatch = _perm("marketplace.scan_dispatch")
CanConfirmDispatch = _perm("marketplace.confirm_dispatch")
# Cutting a delivery note into a CLOSED month moves stock and value between
# accounting periods, so it is gated separately from the ordinary cut.
CanBackdateDeliveryNote = _perm("marketplace.backdate_delivery_note")
CanCancelDispatch = _perm("marketplace.cancel_dispatch")
CanViewReturn = _perm("marketplace.view_return")
CanAddReturn = _perm("marketplace.add_return")
CanSubmitReturn = _perm("marketplace.submit_return")
CanViewMaster = _perm("marketplace.view_master")
CanChangeMaster = _perm("marketplace.change_master")
CanViewReconciliation = _perm("marketplace.view_reconciliation")
CanGateCheck = _perm("marketplace.gate_check")
# The outward trip. Split from the gate check because they are different jobs:
# the gate check passes the parcels, the gate pass moves the vehicle. Weighing
# and marking out are separated again so a weighbridge operator need not be
# trusted to release a load.
CanViewGatePass = _perm("marketplace.can_view_mp_gate_pass")
CanManageGatePass = _perm("marketplace.can_manage_mp_gate_pass")
CanWeighGatePass = _perm("marketplace.can_weigh_mp_gate_pass")
CanPrintGatePass = _perm("marketplace.can_print_mp_gate_pass")
CanDispatchGatePass = _perm("marketplace.can_dispatch_mp_gate_pass")
# Sheet import + warehouse issue request
CanImportOrders = _perm("marketplace.import_orders")
CanViewBatch = _perm("marketplace.view_batch")
CanSendIssueRequest = _perm("marketplace.send_issue_request")
CanReviewIssueRequest = _perm("marketplace.review_issue_request")
CanIssueMaterials = _perm("marketplace.issue_materials")
CanReceiveIssue = _perm("marketplace.receive_issue")
# Packing
CanViewPacking = _perm("marketplace.view_packing")
CanPackOrder = _perm("marketplace.pack_order")
