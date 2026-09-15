"""Resolve a SAP document number printed in a report back to an app record.

A report row only ever carries SAP's own identifiers -- ``BASE_REF`` on an
inventory movement, a ``DocNum`` elsewhere. The operator reading that row
usually wants the thing this app knows about it: which transfer request raised
it, which branch stock transfer carried it.

The mapping is many-to-many in both directions. One SAP inventory transfer is
both the ``sap_transfer_doc_num`` of a transfer request and the ``sap_doc_num``
of the BST seeded from it, so a single reference can resolve to two records; a
BST that combines several invoices resolves from any one of them.

Nothing here reaches SAP -- it is a lookup across this app's own tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from warehouse.models_bst import BSTTransfer
from warehouse.models_transfer import WarehouseTransferRequest

#: What a reference can point at. The frontend keys its modal off these.
KIND_TRANSFER_REQUEST = "TRANSFER_REQUEST"
KIND_BST = "BST"

#: Permission a user needs before a match of each kind is handed back.
REQUIRED_PERMISSION = {
    KIND_TRANSFER_REQUEST: "warehouse.view_warehousetransferrequest",
    KIND_BST: "warehouse.view_bsttransfer",
}

#: Guards the batch endpoint. A report page resolves one screenful at a time.
MAX_REFERENCES = 500


@dataclass
class ReferenceMatch:
    """One app record a report reference points at."""

    kind: str
    id: int
    entry_no: str
    #: One line naming the record, for the row's tooltip and the modal title.
    summary: str = ""
    #: Which SAP field matched, so the UI can say *why* the row is linked.
    matched_on: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "id": self.id,
            "entry_no": self.entry_no,
            "summary": self.summary,
            "matched_on": self.matched_on,
        }


@dataclass
class _Bucket:
    matches: list[ReferenceMatch] = field(default_factory=list)


def _clean(references) -> list[str]:
    """Distinct, non-empty references as strings, capped.

    Report cells arrive as whatever JSON carried them -- a doc number may be a
    number in one report and a string in the next -- so everything is compared
    as text, which is how both models store it.
    """
    seen: list[str] = []
    known: set[str] = set()
    for raw in references or []:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or text in known:
            continue
        known.add(text)
        seen.append(text)
        if len(seen) >= MAX_REFERENCES:
            break
    return seen


def resolve_references(references, *, company=None, user=None) -> dict[str, list[dict]]:
    """Map each reference to the app records that carry it.

    ``company`` scopes the lookup to the company whose report was run --
    document numbers are only unique within a company database, so an
    unscoped lookup could link a row to another company's record.

    ``user`` filters by view permission: a reader with no BST rights gets the
    transfer-request match and no BST match, rather than a link that dies on
    the route guard.
    """
    wanted = _clean(references)
    if not wanted:
        return {}

    buckets: dict[str, _Bucket] = {reference: _Bucket() for reference in wanted}

    def allowed(kind: str) -> bool:
        if user is None:
            return True
        return user.has_perm(REQUIRED_PERMISSION[kind])

    if allowed(KIND_TRANSFER_REQUEST):
        _collect_transfer_requests(wanted, buckets, company)
    if allowed(KIND_BST):
        _collect_bst(wanted, buckets, company)

    # Only references that actually matched something come back, so the caller
    # can treat "in the map" as "this row is clickable".
    return {
        reference: [match.as_dict() for match in bucket.matches]
        for reference, bucket in buckets.items()
        if bucket.matches
    }


def _collect_transfer_requests(wanted, buckets, company) -> None:
    """A transfer request is reachable by any of its three SAP documents."""
    queryset = WarehouseTransferRequest.objects.filter(
        # The request (OWTQ), the transfer (OWTR), and a cross-branch leg 2 are
        # three different SAP documents on one request -- a movement row can
        # quote whichever of them moved the stock.
        models_q_any_doc(wanted)
    )
    if company is not None:
        queryset = queryset.filter(company=company)

    fields = ("sap_transfer_doc_num", "sap_request_doc_num", "sap_leg2_doc_num")
    labels = {
        "sap_transfer_doc_num": "Inventory Transfer",
        "sap_request_doc_num": "Transfer Request",
        "sap_leg2_doc_num": "Inventory Transfer (leg 2)",
    }
    for request in queryset.only(
        "id", "entry_no", "from_warehouse", "to_warehouse", *fields
    ):
        summary = f"{request.from_warehouse} → {request.to_warehouse}"
        for name in fields:
            value = getattr(request, name, "") or ""
            if value in buckets:
                buckets[value].matches.append(
                    ReferenceMatch(
                        kind=KIND_TRANSFER_REQUEST,
                        id=request.id,
                        entry_no=request.entry_no,
                        summary=summary,
                        matched_on=labels[name],
                    )
                )


def _collect_bst(wanted, buckets, company) -> None:
    """A BST is reachable by its own doc number or any document it combines."""
    from django.db.models import Q

    queryset = BSTTransfer.objects.filter(
        Q(sap_doc_num__in=wanted) | Q(docs__sap_doc_num__in=wanted)
    ).distinct()
    if company is not None:
        queryset = queryset.filter(company=company)

    for transfer in queryset.prefetch_related("docs").only(
        "id", "entry_no", "sap_doc_num", "sap_from_warehouse", "sap_to_warehouse"
    ):
        route = " → ".join(
            part for part in (transfer.sap_from_warehouse, transfer.sap_to_warehouse) if part
        )
        # A combined BST answers to every document number it carries, not just
        # the leader's -- that is the whole point of combining them.
        numbers = {transfer.sap_doc_num or ""}
        numbers.update(doc.sap_doc_num or "" for doc in transfer.docs.all())
        for number in numbers:
            if number in buckets:
                buckets[number].matches.append(
                    ReferenceMatch(
                        kind=KIND_BST,
                        id=transfer.id,
                        entry_no=transfer.entry_no,
                        summary=route,
                        matched_on="Branch stock transfer",
                    )
                )


def models_q_any_doc(wanted):
    """``Q`` matching a transfer request on any of its SAP document numbers."""
    from django.db.models import Q

    return (
        Q(sap_transfer_doc_num__in=wanted)
        | Q(sap_request_doc_num__in=wanted)
        | Q(sap_leg2_doc_num__in=wanted)
    )
