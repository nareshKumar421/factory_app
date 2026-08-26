"""Compare what the app believes about a transfer against what SAP holds.

Every finding here is a way the two can drift apart, and each one has a cost:

* a reservation the app thinks it released but SAP still holds is stock nobody
  can use — the failure mode that left 484 requests stale across the three
  companies before the app was involved at all
* a transfer stuck in transit is stock that exists in a bookkeeping warehouse
  and nowhere real
* a quantity mismatch means the app's idea of what moved is wrong, which makes
  every downstream number wrong too

The point is that drift is *reported* rather than discovered months later, so
this is meant to be run on a schedule and read.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from django.utils import timezone

from ..models_transfer import (
    TransferPostingStatus,
    TransferRequestStatus,
    TransferRouteType,
    WarehouseTransferRequest,
)

logger = logging.getLogger(__name__)

# How long something may sit in a waiting state before it counts as drift.
STUCK_IN_TRANSIT_DAYS = 7
AWAITING_POST_DAYS = 3

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class Finding:
    entry_no: str
    request_id: int
    severity: str
    code: str
    message: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class TransferReconciler:
    """Reconcile app transfer requests against their SAP documents."""

    def __init__(self, service):
        # Takes the TransferRequestService rather than a company code so it
        # shares the already-built SAP client and branch map.
        self.service = service

    def run(self, *, include_settled: bool = False, limit: int = 500) -> dict:
        requests = self._candidates(include_settled=include_settled, limit=limit)
        if not requests:
            return {"checked": 0, "findings": [], "summary": {}}

        doc_entries = [
            r.sap_request_doc_entry for r in requests if r.sap_request_doc_entry
        ]
        sap = (
            self.service.client.summarise_transfer_requests(doc_entries)
            if doc_entries else {}
        )

        findings: list[Finding] = []
        for request in requests:
            findings.extend(self._check(request, sap.get(request.sap_request_doc_entry)))

        findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.entry_no))

        summary: dict[str, int] = {}
        for finding in findings:
            summary[finding.code] = summary.get(finding.code, 0) + 1

        return {
            "checked": len(requests),
            "findings": [f.as_dict() for f in findings],
            "summary": summary,
        }

    # ------------------------------------------------------------------

    def _candidates(self, *, include_settled: bool, limit: int):
        qs = self.service.base_queryset()
        if not include_settled:
            # A request that is posted and whose reservation is released has
            # nothing left to drift, so skip it unless asked for everything.
            qs = qs.exclude(
                posting_status=TransferPostingStatus.POSTED,
                sap_request_closed_at__isnull=False,
            )
        return list(qs.order_by("created_at")[:max(1, int(limit))])

    def _check(self, request: WarehouseTransferRequest, sap: dict | None) -> list[Finding]:
        findings: list[Finding] = []

        def add(severity, code, message, **detail):
            findings.append(Finding(
                entry_no=request.entry_no, request_id=request.id,
                severity=severity, code=code, message=message, detail=detail,
            ))

        age_days = (timezone.now() - request.created_at).days

        # --- the SAP request ------------------------------------------------
        if request.sap_request_doc_entry and sap is None:
            add("critical", "sap_request_missing",
                f"The app holds SAP request {request.sap_request_doc_num or request.sap_request_doc_entry} "
                f"but SAP has no such document. Someone may have deleted it.",
                sap_doc_entry=request.sap_request_doc_entry)
            return findings

        if not request.sap_request_doc_entry:
            add("warning", "no_sap_request",
                "This request was never mirrored into SAP, so it is reserving "
                "nothing — the source warehouse could promise the same stock twice.")

        if sap:
            reservation_live = sap["is_open"]

            if reservation_live and request.status in (
                TransferRequestStatus.REJECTED, TransferRequestStatus.CANCELLED
            ):
                add("critical", "reservation_not_released",
                    f"The app {request.get_status_display().lower()} this "
                    f"{age_days} days ago but SAP still reserves "
                    f"{sap['open_quantity']} across {sap['open_lines']} line(s). "
                    f"Closing the request must have failed.",
                    open_quantity=str(sap["open_quantity"]),
                    open_lines=sap["open_lines"])

            if reservation_live and request.posting_status == TransferPostingStatus.POSTED:
                add("warning", "reservation_open_after_posting",
                    f"The transfer is posted but SAP still shows "
                    f"{sap['open_quantity']} open on the request. The remainder "
                    f"is reserved and will not be served.",
                    open_quantity=str(sap["open_quantity"]))

            if not reservation_live and request.status == TransferRequestStatus.PENDING:
                add("warning", "sap_closed_while_pending",
                    "SAP shows the request closed, but the app is still waiting "
                    "for a decision — approving it now would reserve nothing.",
                    doc_status=sap["doc_status"], cancelled=sap["cancelled"])

            # Only meaningful once the app has actually posted something. SAP's
            # served figure is derived as total minus open, and `Close` zeroes
            # OpenQty — so a request closed without being served reads as fully
            # served, which would make every rejection look like a mismatch.
            if request.posting_status in (
                TransferPostingStatus.IN_TRANSIT, TransferPostingStatus.POSTED
            ):
                expected_served = sum(
                    (line.transferred_qty for line in request.lines.all()), Decimal("0")
                )
                if expected_served != sap["served_quantity"]:
                    add("warning", "quantity_mismatch",
                        f"The app recorded {expected_served} as transferred but SAP "
                        f"served {sap['served_quantity']} against the request.",
                        app_transferred=str(expected_served),
                        sap_served=str(sap["served_quantity"]))

        # --- the transfer itself --------------------------------------------
        if request.posting_status == TransferPostingStatus.FAILED:
            add("critical", "posting_failed",
                f"Posting to SAP failed and has not been retried: "
                f"{request.posting_error or 'no message recorded'}",
                posting_error=request.posting_error)

        if (
            request.route_type == TransferRouteType.CROSS_BRANCH
            and request.posting_status == TransferPostingStatus.IN_TRANSIT
        ):
            in_transit_days = (
                (timezone.now() - request.posted_at).days if request.posted_at else age_days
            )
            if in_transit_days >= STUCK_IN_TRANSIT_DAYS:
                add("critical", "stuck_in_transit",
                    f"Stock has been sitting in {request.intransit_warehouse} for "
                    f"{in_transit_days} days. It exists in SAP but not at any real "
                    f"warehouse until the second leg posts.",
                    intransit_warehouse=request.intransit_warehouse,
                    days=in_transit_days)

        if (
            request.is_approved
            and request.posting_status == TransferPostingStatus.NOT_POSTED
        ):
            approved_days = (
                (timezone.now() - request.reviewed_at).days if request.reviewed_at else age_days
            )
            if approved_days >= AWAITING_POST_DAYS:
                add("info", "approved_but_not_posted",
                    f"Approved {approved_days} days ago and still not posted. The "
                    f"reservation is holding stock nobody has moved.",
                    days=approved_days)

        if request.bst_transfer_id is None and request.sap_transfer_doc_entry:
            add("info", "no_bst",
                "The transfer is posted in SAP but no BST was created, so nothing "
                "checked the boxes that physically moved.")

        return findings
