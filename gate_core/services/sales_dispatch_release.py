"""Settle a docking's bill bindings when it is cancelled or rejected.

Cancel/reject abort a docking *before* dispatch. Per the dispatch design (A7),
the bills should fall back to their gate-in stage and stay re-dockable on the
same truck -- they are NOT released to Expected Dispatch (that is C1
remove-a-bill's job, which un-links + voids the cover for a *future* trip).

The only real defect the audit found is that the cancelled/rejected docking's
``SalesDispatchGateOutDocument`` rows keep their ``dispatch_plan`` FK, so the
pipeline "Vehicle Status" derivation still reads the dead docking's stage
(showing REJECTED forever). Detaching those FKs -- plus the header's own
``dispatch_plan`` -- lets each plan derive from its live gate-in again
(READY_TO_DOCK) so it can be re-docked.
"""

from django.utils import timezone


def settle_cancelled_docking(entry, user):
    """Detach a cancelled/rejected docking's bills and reverse any consumed covers.

    Nulls the header + document ``dispatch_plan`` FKs so the bills stop deriving
    the dead docking's stage, and calls ``unconsume_covers_for_plans`` (a no-op
    when reject/cancel precede dispatch, correct for un-dock-after-dispatch). The
    plans keep their ``linked_vehicle_entry`` + covers, so they revert to
    "ready to dock" on the still-present truck. The caller persists ``entry``.

    Returns the affected plan list.
    """
    from gate_core.models import SalesDispatchGateOutDocument
    from gate_core.services.empty_vehicle_dispatch import unconsume_covers_for_plans

    documents = list(
        entry.documents.filter(is_active=True).select_related("dispatch_plan")
    )
    plans = [document.dispatch_plan for document in documents if document.dispatch_plan_id]
    if entry.dispatch_plan_id and all(entry.dispatch_plan_id != p.id for p in plans):
        plans.append(entry.dispatch_plan)

    now = timezone.now()
    SalesDispatchGateOutDocument.objects.filter(
        sales_dispatch=entry, is_active=True, dispatch_plan__isnull=False
    ).update(dispatch_plan=None, updated_by=user, updated_at=now)
    # Header's direct FK (the primary bill's other derivation path).
    entry.dispatch_plan = None

    unconsume_covers_for_plans(plans, user)
    return plans
