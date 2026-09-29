"""
admin_board/dispatch_bills.py

The bills behind the Total dispatch tile, and the one query both read.

THE SAME ROWS AS THE TILE
-------------------------
``dispatched_gate_outs`` is the tile's own filter — trucks that left the gate in
the window, intercompany customers excluded per company — lifted out of
``AdminBoardService._dispatch`` so that the company row and the bill list under
it are built from one queryset. A bill list that filtered for itself would be
free to add up to a different tonnage than the row that opened it.

A GATE-OUT IS A TRUCK, NOT A BILL
---------------------------------
One ``SalesDispatchGateOut`` row is one company's load on one truck, and it can
carry several SAP invoices: on 29 Sep 2026 one Mart truck left with seven. The
invoices themselves are ``SalesDispatchGateOutDocument`` rows under it, each
with its own date, weight and value, and the active ones add up to the gate-out
exactly (measured on live, September 2026: 0 of 225 gate-outs disagree).

So a bill is counted from the documents, never from the gate-out's
``dispatch_plan`` link, which names only the first bill on the truck. Counting
that link reported 225 bills for September when 629 had gone out.

A bill split across two trucks appears once per truck, each row carrying that
truck's share of it, and is counted once.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Dict, List, Optional

from django.db.models import Prefetch, QuerySet

from gate_core.models.sales_dispatch import (
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
)

from .constants import INTERCOMPANY_CARD_CODES


def _f(value) -> float:
    return float(value) if value is not None else 0.0


def dispatched_gate_outs(company_ids: Dict[int, str], date_from: date, date_to: date) -> QuerySet:
    """Trucks that left the gate in the window, for these companies.

    ``company_ids`` maps a company's id to its code, because the intercompany
    list is per company: ``CUSTA000906`` is a group company to Oil and a real
    customer to Mart, so one merged list wrongly strips genuine Mart sales.
    """
    rows = SalesDispatchGateOut.objects.filter(
        company_id__in=list(company_ids),
        gate_out_date__range=(date_from, date_to),
        status=SalesDispatchGateOutStatus.DISPATCHED,
    )
    for company_id, code in company_ids.items():
        excluded = INTERCOMPANY_CARD_CODES.get(code, [])
        if excluded:
            rows = rows.exclude(company_id=company_id, customer_code__in=excluded)
    return rows


def count_bills(gate_outs: QuerySet) -> int:
    """Distinct SAP bills on these trucks.

    Distinct on the company as well as the document: Oil and Mart number their
    invoices in separate schemas, so the same DocEntry in both is two bills. A
    gate-out with no document rows at all is counted as its own one bill, so
    that a truck docked before documents were recorded still counts.
    """
    documented = (
        SalesDispatchGateOutDocument.objects.filter(sales_dispatch__in=gate_outs, is_active=True)
        .values("company_id", "document_type", "sap_doc_entry")
        .distinct()
        .count()
    )
    undocumented = gate_outs.exclude(documents__is_active=True).count()
    return documented + undocumented


def company_bills(company_id: int, company_code: str, date_from: date, date_to: date) -> Dict[str, Any]:
    """Every bill one company dispatched in the window, newest truck first."""
    gate_outs = list(
        dispatched_gate_outs({company_id: company_code}, date_from, date_to)
        .prefetch_related(
            Prefetch(
                "documents",
                queryset=SalesDispatchGateOutDocument.objects.filter(is_active=True).order_by(
                    "sap_doc_date", "id"
                ),
                to_attr="bill_documents",
            )
        )
        .order_by("-gate_out_date", "-out_time", "-id")
    )

    rows: List[Dict[str, Any]] = []
    for gate_out in gate_outs:
        # The gate-out stands in for its own bill when it has no document rows,
        # which keeps the list adding up to the tile whatever the data's age.
        documents = gate_out.bill_documents or [None]
        for document in documents:
            rows.append(_bill_row(gate_out, document, len(documents), date_from))

    # A bill on two trucks is one bill, and each of its rows says so.
    trucks_per_bill = Counter(row["_bill"] for row in rows)
    for row in rows:
        row["trucks_for_bill"] = trucks_per_bill[row.pop("_bill")]

    earlier = [row for row in rows if row["billed_before_month"]]
    waits = [row["days_to_dispatch"] for row in rows if row["days_to_dispatch"] is not None]

    return {
        "company_code": company_code,
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "bills": len(trucks_per_bill),
        "trucks": len(gate_outs),
        # Summed from the gate-outs, as the tile sums them, rather than from the
        # rows: the two agree, and this way agreement is not a matter of rounding.
        "tons": round(sum(_f(g.total_weight) for g in gate_outs) / 1000, 2),
        "amount": round(sum(row["amount"] or 0 for row in rows), 2),
        "earlier_bills": {
            "bills": len({(row["document_type"], row["sap_doc_entry"]) for row in earlier}),
            "tons": round(sum(row["tons"] for row in earlier), 2),
        },
        # Rows whose weight is nil in the register. They are real dispatches
        # whose tonnes are missing from the tile, not empty trucks, and the list
        # names them so the gap can be traced to the item master.
        "unweighed_bills": sum(1 for row in rows if not row["weighed"]),
        "split_bills": sum(1 for n in trucks_per_bill.values() if n > 1),
        "avg_days_to_dispatch": round(sum(waits) / len(waits), 1) if waits else None,
        "rows": rows,
    }


def _bill_row(
    gate_out: SalesDispatchGateOut,
    document: Optional[SalesDispatchGateOutDocument],
    bills_on_truck: int,
    month_first: date,
) -> Dict[str, Any]:
    source = document or gate_out
    bill_date = source.sap_doc_date
    weight = _f(source.total_weight)
    return {
        "key": f"{gate_out.id}:{document.id if document else 0}",
        # Private: the identity a split bill shares across trucks. Removed
        # before the row leaves this module.
        "_bill": (source.document_type, source.sap_doc_entry),
        "document_type": source.document_type,
        "sap_doc_entry": source.sap_doc_entry,
        "bill_no": source.sap_doc_num or str(source.sap_doc_entry),
        "bill_date": bill_date.isoformat() if bill_date else None,
        "dispatch_date": gate_out.gate_out_date.isoformat() if gate_out.gate_out_date else None,
        "out_time": gate_out.out_time.strftime("%H:%M") if gate_out.out_time else None,
        "days_to_dispatch": (
            (gate_out.gate_out_date - bill_date).days
            if bill_date and gate_out.gate_out_date
            else None
        ),
        "billed_before_month": bool(bill_date and bill_date < month_first),
        "customer_code": source.customer_code or gate_out.customer_code or "",
        "customer_name": source.customer_name or gate_out.customer_name or "",
        "place_of_supply": source.place_of_supply or gate_out.place_of_supply or "",
        "tons": round(weight / 1000, 3),
        "weighed": weight > 0,
        "boxes": _f(source.total_boxes),
        "amount": _f(source.sap_doc_total) if source.sap_doc_total is not None else None,
        "eway_bill": source.eway_bill or gate_out.eway_bill or "",
        "vehicle_no": gate_out.vehicle_no or "",
        "transporter_name": gate_out.transporter_name or "",
        "driver_name": gate_out.driver_name or "",
        "driver_mobile_no": gate_out.driver_mobile_no or "",
        "gatepass_no": gate_out.gatepass_no or "",
        "bills_on_truck": bills_on_truck,
    }
