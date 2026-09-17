"""
universal_search/services/app_lookup.py

What this app itself knows about a number.

SAP answers "there is an invoice 626090411". This answers the question the
person at the screen actually has: *which of our records touched it* -- the
docking that carried it, the branch transfer that moved it, the return that
brought it back. A number that SAP has never heard of may still be one of our
own entry numbers, so those are matched here too.

Every source is filtered twice. Once by company, because an entry number means
nothing outside the company that issued it, and once by the **view permission
of the module that owns the record**: a user with no BST rights gets the
transfer-request hit and no BST hit, rather than a link that dies on the route
guard. That second filter is the reason this module can be opened widely.

Adding a source is adding one ``AppSource`` below. Nothing else changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from django.db.models import Q

logger = logging.getLogger(__name__)

#: Records returned per source per company. A number that matches more than a
#: handful of records is a number that means something else.
MAX_HITS_PER_SOURCE = 10


@dataclass(frozen=True)
class AppHit:
    """One app record the term pointed at."""

    kind: str
    label: str
    id: int
    entry_no: str
    summary: str
    status: str
    #: Which field matched, so the modal can say *why* this came back.
    matched_on: str
    #: Where to go to see it. Some modules have no per-record page yet; those
    #: link to the list that holds it rather than pretending to a deep link.
    route: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "id": self.id,
            "entry_no": self.entry_no,
            "summary": self.summary,
            "status": self.status,
            "matched_on": self.matched_on,
            "route": self.route,
        }


@dataclass(frozen=True)
class AppSource:
    """One app model the universal search looks in."""

    kind: str
    label: str
    #: Django permission the viewer needs before this source is even queried.
    permission: str
    #: Dotted lookup from the model to its ``Company``.
    company_path: str
    #: ``{field: how the match is described}``. Text columns; matched exactly.
    text_fields: dict[str, str]
    #: Same, for integer columns -- skipped unless the term is all digits,
    #: because Django raises rather than returning nothing when a non-numeric
    #: string is compared against an integer column.
    number_fields: dict[str, str] = field(default_factory=dict)
    #: Called with the model, returns the queryset to search. Deferred so the
    #: registry can be declared before Django's app registry is ready.
    model: Callable[[], Any] = None
    #: ``(record) -> (entry_no, summary, status)``.
    describe: Callable[[Any], tuple[str, str, str]] = None
    #: Format string over the record's id.
    route: str = ""
    #: Fields to prefetch so ``describe`` does not go back to the database.
    select_related: tuple[str, ...] = ()
    prefetch_related: tuple[str, ...] = ()


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------


def _transfer_request_model():
    from warehouse.models_transfer import WarehouseTransferRequest

    return WarehouseTransferRequest


def _bst_model():
    from warehouse.models_bst import BSTTransfer

    return BSTTransfer


def _dispatch_plan_model():
    from dispatch_plans.models import DispatchPlan

    return DispatchPlan


def _grpo_model():
    from grpo.models import GRPOPosting

    return GRPOPosting


def _service_grpo_model():
    from grpo.models import ServiceGRPOPosting

    return ServiceGRPOPosting


def _goods_return_model():
    from goods_return.models import GoodsReturn

    return GoodsReturn


def _short_dispatch_model():
    from short_dispatch.models import ShortDispatch

    return ShortDispatch


def _ar_invoice_model():
    from ar_invoice.models import ARInvoicePosting

    return ARInvoicePosting


def _describe_transfer_request(record) -> tuple[str, str, str]:
    route = " -> ".join(
        part for part in (record.from_warehouse, record.to_warehouse) if part
    )
    return record.entry_no, route, record.status


def _describe_bst(record) -> tuple[str, str, str]:
    route = " -> ".join(
        part for part in (record.sap_from_warehouse, record.sap_to_warehouse) if part
    )
    return record.entry_no, route, record.status


def _describe_dispatch_plan(record) -> tuple[str, str, str]:
    bill = record.sap_invoice_doc_num or str(record.sap_invoice_doc_entry)
    return f"Bill {bill}", record.customer_name or record.customer_code, record.status


def _describe_grpo(record) -> tuple[str, str, str]:
    entry = record.vehicle_entry
    vehicle = getattr(getattr(entry, "vehicle", None), "vehicle_number", "") or ""
    return (
        f"GRPO {record.sap_doc_num or record.id}",
        vehicle,
        record.status,
    )


def _describe_service_grpo(record) -> tuple[str, str, str]:
    return (
        f"Service GRPO {record.sap_doc_num or record.id}",
        record.vendor_name or record.vendor_code,
        record.status,
    )


def _describe_goods_return(record) -> tuple[str, str, str]:
    return record.entry_no, record.customer_name or "", record.status


def _describe_short_dispatch(record) -> tuple[str, str, str]:
    return record.entry_no, record.customer_name or "", record.status


def _describe_ar_invoice(record) -> tuple[str, str, str]:
    return (
        f"Invoice {record.sap_doc_num or record.id}",
        record.customer_name or record.customer_code,
        record.status,
    )


APP_SOURCES: tuple[AppSource, ...] = (
    AppSource(
        kind="TRANSFER_REQUEST",
        label="Transfer request",
        permission="warehouse.view_warehousetransferrequest",
        company_path="company",
        # A request is reachable by any of its three SAP documents: the request
        # itself, the transfer it became, and a cross-branch second leg.
        text_fields={
            "entry_no": "Entry no",
            "sap_request_doc_num": "Transfer request",
            "sap_transfer_doc_num": "Inventory transfer",
            "sap_leg2_doc_num": "Inventory transfer (leg 2)",
        },
        model=_transfer_request_model,
        describe=_describe_transfer_request,
        route="/warehouse/inventory-transfer/{id}",
    ),
    AppSource(
        kind="BST",
        label="Branch stock transfer",
        permission="warehouse.view_bsttransfer",
        company_path="company",
        # A combined BST answers to every document it carries, not just the
        # leader's -- that is the whole point of combining them.
        text_fields={
            "entry_no": "Entry no",
            "sap_doc_num": "Inventory transfer",
            "docs__sap_doc_num": "Invoice on the transfer",
        },
        model=_bst_model,
        describe=_describe_bst,
        route="/warehouse/bst/{id}",
    ),
    AppSource(
        kind="DISPATCH_PLAN",
        label="Dispatch plan",
        permission="dispatch_plans.view_dispatchplan",
        company_path="company",
        text_fields={"sap_invoice_doc_num": "Invoice"},
        number_fields={"sap_invoice_doc_entry": "Invoice DocEntry"},
        model=_dispatch_plan_model,
        describe=_describe_dispatch_plan,
        route="/dispatch/plans",
    ),
    AppSource(
        kind="GRPO",
        label="GRPO posting",
        permission="grpo.view_grpoposting",
        company_path="vehicle_entry__company",
        text_fields={},
        number_fields={"sap_doc_num": "GRPO"},
        model=_grpo_model,
        describe=_describe_grpo,
        route="/warehouse/grpo/material/history/{id}",
        select_related=("vehicle_entry", "vehicle_entry__vehicle"),
    ),
    AppSource(
        kind="SERVICE_GRPO",
        label="Service GRPO posting",
        permission="grpo.view_servicegrpoposting",
        company_path="dispatch_plan__company",
        text_fields={},
        number_fields={"sap_doc_num": "Service GRPO"},
        model=_service_grpo_model,
        describe=_describe_service_grpo,
        route="/warehouse/grpo/service/history/{id}",
    ),
    AppSource(
        kind="GOODS_RETURN",
        label="Customer return",
        permission="goods_return.view_goodsreturn",
        company_path="company",
        text_fields={
            "entry_no": "Entry no",
            "sap_gr_doc_num": "A/R Return",
            "invoice_refs__sap_invoice_doc_num": "Invoice on the return",
            "invoice_refs__sap_gr_doc_num": "A/R Return",
        },
        model=_goods_return_model,
        describe=_describe_goods_return,
        route="/returns/customer/{id}",
    ),
    AppSource(
        kind="SHORT_DISPATCH",
        label="Short dispatch",
        permission="short_dispatch.view_shortdispatch",
        company_path="company",
        text_fields={
            "entry_no": "Entry no",
            "sap_invoice_doc_num": "Invoice",
            "sap_return_doc_num": "A/R Return",
        },
        model=_short_dispatch_model,
        describe=_describe_short_dispatch,
        route="/warehouse/short-dispatch",
    ),
    AppSource(
        kind="AR_INVOICE_POSTING",
        label="A/R invoice raised here",
        permission="ar_invoice.view_arinvoiceposting",
        company_path="company",
        text_fields={},
        number_fields={"sap_doc_num": "Invoice"},
        model=_ar_invoice_model,
        describe=_describe_ar_invoice,
        route="/warehouse/ar-invoices",
    ),
)


# ----------------------------------------------------------------------
# Searching
# ----------------------------------------------------------------------


def search_app_records(term: str, *, company, user) -> list[dict[str, Any]]:
    """Every app record in one company that carries this term.

    ``user`` is required, not optional: this is what stops the search handing
    back a record the caller has no right to open.
    """
    term = (term or "").strip()
    if not term:
        return []

    hits: list[AppHit] = []
    for source in APP_SOURCES:
        if not user.has_perm(source.permission):
            continue
        try:
            hits.extend(_search_source(source, term, company))
        except Exception:
            # One module's model going wrong must not take the whole search
            # down with it -- the other seven still have an answer.
            logger.exception(
                "Universal search source %s failed for %r", source.kind, term
            )
    return [hit.as_dict() for hit in hits]


def _search_source(source: AppSource, term: str, company) -> Iterable[AppHit]:
    fields = dict(source.text_fields)
    if term.isdigit():
        fields.update(source.number_fields)
    if not fields:
        return []

    condition = Q()
    for name in fields:
        condition |= Q(**{name: term})

    queryset = source.model().objects.filter(condition)
    if company is not None:
        queryset = queryset.filter(**{source.company_path: company})
    if source.select_related:
        queryset = queryset.select_related(*source.select_related)
    if source.prefetch_related:
        queryset = queryset.prefetch_related(*source.prefetch_related)
    # A join through a child row (a BST's invoices, a return's bills) repeats
    # the parent once per matching child.
    queryset = queryset.distinct()[:MAX_HITS_PER_SOURCE]

    hits = []
    for record in queryset:
        entry_no, summary, status = source.describe(record)
        hits.append(
            AppHit(
                kind=source.kind,
                label=source.label,
                id=record.id,
                entry_no=entry_no,
                summary=summary,
                status=status or "",
                matched_on=_matched_on(record, fields, term),
                route=source.route.format(id=record.id),
            )
        )
    return hits


def _matched_on(record, fields: dict[str, str], term: str) -> str:
    """Which of the source's fields actually held the term.

    Re-derived from the record rather than from the query, because a single
    ``Q`` with four ORs cannot say which branch matched. Fields that reach
    through a relation are not walked here -- they are reported by their
    label, which is what the label is for.
    """
    for name, label in fields.items():
        if "__" in name:
            continue
        value = getattr(record, name, None)
        if value is not None and str(value) == term:
            return label
    # Everything left is a related-row match; the first such label is the only
    # honest answer without re-querying.
    related = [label for name, label in fields.items() if "__" in name]
    return related[0] if related else ""
