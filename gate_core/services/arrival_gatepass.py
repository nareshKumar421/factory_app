"""Combined gate-exit gatepass for a multi-company truck.

One physical truck trip (``VehicleArrival``) gets ONE ``ARV/...`` gatepass that
spans its per-company dockings. Printing it also runs each docking's own
``assign_gatepass`` so the per-company ``DCK/...`` numbers, QR, status, and the
ORIGINAL print-log audit are still produced for SAP/GST records. Single-company
trucks keep using the per-docking gatepass flow.
"""

import secrets

from django.db import transaction
from django.utils import timezone

# Dockings open enough to ride a combined gatepass (excludes rejected/cancelled).
_ACTIVE_DOCKING_STATUSES = (
    "DOCKED",
    "PHOTO_ATTACHED",
    "READY_FOR_GATEPASS",
    "GATEPASS_PRINTED",
    "PRINT_COMMITTED",
    "DISPATCHED",
)
_ALREADY_PRINTED_STATUSES = ("GATEPASS_PRINTED", "PRINT_COMMITTED", "DISPATCHED")


def arrival_dockings(arrival):
    """Active, in-flight dockings on the trip (excludes rejected/cancelled)."""
    return list(
        arrival.gate_outs.filter(
            is_active=True, status__in=_ACTIVE_DOCKING_STATUSES
        ).select_related("company")
    )


def locked_companies(dockings):
    """Codes of any involved company whose docking printing is on hold."""
    from gate_core.models import SalesDispatchLock

    codes = []
    for docking in dockings:
        lock = SalesDispatchLock.for_company(docking.company)
        if lock and lock.is_locked and docking.company.code not in codes:
            codes.append(docking.company.code)
    return codes


def get_arrival_gatepass_readiness(arrival):
    """Per-company readiness + whether the whole combined gatepass can print."""
    from gate_core.services.sales_dispatch_gatepass import get_gatepass_readiness

    dockings = arrival_dockings(arrival)
    companies = []
    all_ready = bool(dockings)
    for docking in dockings:
        if docking.status in _ALREADY_PRINTED_STATUSES:
            ready, missing = True, []
        else:
            readiness = get_gatepass_readiness(docking)
            ready, missing = readiness["ready"], readiness.get("missing", [])
        if not ready:
            all_ready = False
        companies.append(
            {
                "docking_id": docking.id,
                "entry_no": docking.entry_no,
                "company_code": docking.company.code,
                "company_name": docking.company.name,
                "status": docking.status,
                "ready": ready,
                "missing": missing,
                "gatepass_no": docking.gatepass_no,
            }
        )
    locked = locked_companies(dockings)
    return {
        "ready": all_ready and not locked,
        "companies": companies,
        "locked_companies": locked,
        "arrival_gatepass_no": arrival.gatepass_no,
    }


def assign_arrival_gatepass(arrival, user, *, printer_name="", ip_address=None, user_agent=""):
    """Assign the combined ``ARV/...`` number and print each company's docking.

    Idempotent on the arrival number; per docking it prints only those without an
    existing original. Raises ``ValueError`` if a docking isn't ready. The caller
    must have already confirmed that no involved company is locked.
    """
    from gate_core.models import (
        ArrivalGatepassSequence,
        SalesDispatchGatepassPrintLog,
        SalesDispatchGatepassPrintType,
    )
    from gate_core.services.sales_dispatch_gatepass import ensure_gatepass_ready
    from gate_core.views_sales_dispatch import ensure_partial_dispatch_cleared

    dockings = arrival_dockings(arrival)
    if not dockings:
        raise ValueError("No dockings to print on this arrival.")

    with transaction.atomic():
        if not arrival.gatepass_no:
            arrival.gatepass_no = ArrivalGatepassSequence.next_gatepass_no()
            arrival.gatepass_random_code = secrets.token_urlsafe(9)
        arrival.gatepass_printed_by = user
        arrival.gatepass_printed_at = timezone.now()
        arrival.updated_by = user
        arrival.save(
            update_fields=[
                "gatepass_no",
                "gatepass_random_code",
                "gatepass_printed_by",
                "gatepass_printed_at",
                "updated_by",
                "updated_at",
            ]
        )
        for docking in dockings:
            if docking.gatepass_no or docking.printed_at:
                continue  # already has its own original DCK/... gatepass
            ensure_gatepass_ready(docking)
            ensure_partial_dispatch_cleared(docking)
            docking.assign_gatepass(user)
            SalesDispatchGatepassPrintLog.record_print(
                sales_dispatch=docking,
                print_type=SalesDispatchGatepassPrintType.ORIGINAL,
                user=user,
                printer_name=printer_name,
                ip_address=ip_address,
                user_agent=user_agent,
            )
    return arrival


def commit_arrival_gatepass(arrival, user):
    """Commit each docking's print (GATEPASS_PRINTED -> PRINT_COMMITTED)."""
    from gate_core.models import SalesDispatchGateOutStatus

    if not arrival.gatepass_no:
        raise ValueError("The combined gatepass has not been printed yet.")
    now = timezone.now()
    with transaction.atomic():
        for docking in arrival_dockings(arrival):
            if docking.status == SalesDispatchGateOutStatus.GATEPASS_PRINTED:
                docking.status = SalesDispatchGateOutStatus.PRINT_COMMITTED
                docking.print_committed_by = user
                docking.print_committed_at = now
                docking.updated_by = user
                docking.save(
                    update_fields=[
                        "status",
                        "print_committed_by",
                        "print_committed_at",
                        "updated_by",
                        "updated_at",
                    ]
                )
        arrival.gatepass_committed_by = user
        arrival.gatepass_committed_at = now
        arrival.updated_by = user
        arrival.save(
            update_fields=[
                "gatepass_committed_by",
                "gatepass_committed_at",
                "updated_by",
                "updated_at",
            ]
        )
    return arrival


def reprint_arrival_gatepass(arrival, user, reason, *, printer_name="", ip_address=None, user_agent=""):
    """Log a REPRINT for each printed docking under the combined gatepass."""
    from gate_core.models import (
        SalesDispatchGatepassPrintLog,
        SalesDispatchGatepassPrintType,
    )

    if not arrival.gatepass_no:
        raise ValueError("The combined gatepass has not been printed yet.")
    with transaction.atomic():
        for docking in arrival_dockings(arrival):
            if not docking.gatepass_no:
                continue
            SalesDispatchGatepassPrintLog.record_print(
                sales_dispatch=docking,
                print_type=SalesDispatchGatepassPrintType.REPRINT,
                user=user,
                reprint_reason=reason,
                printer_name=printer_name,
                ip_address=ip_address,
                user_agent=user_agent,
            )
    return arrival
