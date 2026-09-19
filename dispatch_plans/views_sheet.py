"""The Dispatch Sheet — the outward register the office has kept in Excel.

For years the dispatch desk has typed a workbook: one tab for oil, one for
water, a block per day, and a line per invoice that left the gate carrying the
truck, the bilty, the litres and what the freight cost. Every one of those
columns is already in the app — the plan holds the vehicle, transporter,
bilty, priority, kanta weight and freight; the invoice holds the date, the
party, the address and the litres — so the sheet is not a new book to keep.
It is the same rows, laid out the way the desk reads them.

Read-only, deliberately. A figure typed here would be a second, disagreeing
copy of something the plan already says; the way to change a line on this
sheet is to change the plan it is a view of.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.models import Company
from company.permissions import HasCompanyContext
from gate_core.services.user_scope import user_company_ids, wants_all_companies
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .models import DispatchPlan, DispatchPlanStatus
from .permissions import CanViewDispatchSheet
from .serializers import DispatchSheetFilterSerializer
from .services import DispatchPlansService, infer_product_variety

#: The two sheets the workbook has always had. A row is on the water sheet if
#: what it carried was water or a beverage; everything else is oil.
STREAM_OIL = "OIL"
STREAM_WATER = "WATER"


def _decimal(value) -> float | None:
    """A number the sheet can total, or None for a cell that is genuinely empty.

    Zero is a real figure here (a nil-litre line exists on the real sheet, as
    the second bill of a split load), so only None means blank.
    """
    if value in (None, ""):
        return None
    return float(value)


def _stream_of(plan: DispatchPlan, item_summary: str) -> str:
    """Which sheet a row belongs on.

    Read live off what the invoice actually carried, falling back to the
    variety stamped on the plan when SAP is not answering. Live first, because
    ``product_variety`` is stamped once when the plan is made and plans made
    before that existed carry nothing at all.
    """
    if item_summary:
        variety = infer_product_variety(item_summary)
    else:
        variety = plan.product_variety or ""
    return STREAM_WATER if variety.strip().lower().startswith("bever") else STREAM_OIL


class DispatchSheetAPI(APIView):
    """The register for a window of days, one row per invoice dispatched.

    Defaults to the current month, which is the block of the workbook anybody
    opening it is looking for. Every column of the Excel sheet is here; the
    ones SAP owns (invoice date, party, address, litres, boxes) come from one
    query per company for the whole window rather than one per row, and if SAP
    is down the register still loads with those cells empty and
    ``sap_available: false`` saying why.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDispatchSheet]

    def get(self, request):
        filters = DispatchSheetFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filters.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = filters.validated_data

        today = timezone.localdate()
        date_from = data.get("date_from") or today.replace(day=1)
        date_to = data.get("date_to") or today

        companies = self._companies(request)
        plans = self._plans(companies, date_from, date_to, data)

        enrichment, sap_available, sap_error = self._enrich(plans)

        rows = []
        for plan in plans:
            extra = enrichment.get((plan.company_id, plan.sap_invoice_doc_entry)) or {}
            rows.append(self._row(plan, extra))

        stream = data.get("stream", "all")
        if stream in ("oil", "water"):
            wanted = STREAM_OIL if stream == "oil" else STREAM_WATER
            rows = [row for row in rows if row["stream"] == wanted]

        return Response(
            {
                "data": rows,
                "meta": {
                    "total": len(rows),
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "stream": stream,
                    "oil_count": sum(1 for r in rows if r["stream"] == STREAM_OIL),
                    "water_count": sum(1 for r in rows if r["stream"] == STREAM_WATER),
                    "companies": sorted({row["company_code"] for row in rows}),
                    "sap_available": sap_available,
                    "sap_error": sap_error,
                    "fetched_at": timezone.now().isoformat(),
                },
            }
        )

    # -- the rows -------------------------------------------------------------

    @staticmethod
    def _companies(request) -> List[Company]:
        """The companies this read covers.

        One by default — the company in the header. The dispatch desk keeps one
        workbook across the group, though, so ``?all_companies=1`` widens it to
        every company the user belongs to, and each row says which it came from.
        """
        if wants_all_companies(request):
            return list(Company.objects.filter(id__in=user_company_ids(request)))
        return [request.company.company]

    @staticmethod
    def _plans(
        companies: Iterable[Company],
        date_from: date,
        date_to: date,
        data: Dict[str, Any],
    ) -> List[DispatchPlan]:
        plans = DispatchPlan.objects.filter(
            company__in=list(companies),
            dispatch_date__isnull=False,
            dispatch_date__gte=date_from,
            dispatch_date__lte=date_to,
        )

        booking_status = data.get("booking_status", "all")
        if booking_status and booking_status != "all":
            plans = plans.filter(booking_status=booking_status)
        else:
            # A cancelled plan never left the gate, so it is not a line of the
            # register -- ask for it by name to see it.
            plans = plans.exclude(booking_status=DispatchPlanStatus.CANCELLED)

        search = (data.get("search") or "").strip()
        if search:
            plans = plans.filter(
                Q(sap_invoice_doc_num__icontains=search)
                | Q(customer_name__icontains=search)
                | Q(bilty_no__icontains=search)
                | Q(place_of_supply__icontains=search)
                | Q(location__icontains=search)
                | Q(vehicle__vehicle_number__icontains=search)
                | Q(transporter__name__icontains=search)
            )

        return list(
            plans.select_related("company", "vehicle", "transporter", "driver").order_by(
                "dispatch_date", "customer_name", "sap_invoice_doc_num"
            )
        )

    @staticmethod
    def _enrich(plans: List[DispatchPlan]):
        """The invoice half of every row, one SAP query per company.

        Keyed by (company, doc entry) rather than doc entry alone: two
        companies number their invoices independently, so a doc entry is only
        unique inside one of them and a shared key would cross the wires on a
        cross-company read.
        """
        by_company: Dict[int, List[int]] = {}
        codes: Dict[int, str] = {}
        for plan in plans:
            by_company.setdefault(plan.company_id, []).append(plan.sap_invoice_doc_entry)
            codes[plan.company_id] = plan.company.code

        enrichment: Dict[tuple, Dict[str, Any]] = {}
        sap_available = True
        sap_error = ""
        for company_id, doc_entries in by_company.items():
            try:
                service = DispatchPlansService(company_code=codes[company_id])
                for doc_entry, extra in service.get_sheet_enrichment(doc_entries).items():
                    enrichment[(company_id, doc_entry)] = extra
            except (SAPConnectionError, SAPDataError) as exc:
                # One company being unreachable must not blank the others, so
                # the loop carries on and the flag says the sheet is partial.
                sap_available = False
                sap_error = str(exc)
        return enrichment, sap_available, sap_error

    @staticmethod
    def _row(plan: DispatchPlan, extra: Dict[str, Any]) -> Dict[str, Any]:
        """One line of the register, in the workbook's own vocabulary.

        Where the plan and SAP both hold a column, the plan wins: the desk
        edits the plan, and a correction typed there is the whole point of it
        being editable. SAP fills the cell only when the plan's is empty.
        """
        mobile = plan.mobile_no or plan.driver_mobile_no

        return {
            "plan_id": plan.id,
            "sap_invoice_doc_entry": plan.sap_invoice_doc_entry,
            "company_code": plan.company.code,
            "company_name": plan.company.name,
            "stream": _stream_of(plan, extra.get("item_summary", "")),
            "booking_status": plan.booking_status,
            # The workbook's columns, in its order.
            "dispatch_date": plan.dispatch_date.isoformat() if plan.dispatch_date else None,
            "invoice_date": extra.get("invoice_date") or None,
            "party": plan.customer_name or extra.get("card_name", ""),
            "location": plan.location or extra.get("ship_to_address", ""),
            "state": plan.place_of_supply or extra.get("state", ""),
            "invoice_no": plan.sap_invoice_doc_num or str(plan.sap_invoice_doc_entry),
            "bilty_no": plan.bilty_no,
            "bilty_date": plan.bilty_date.isoformat() if plan.bilty_date else None,
            "vehicle_no": plan.vehicle_no,
            "transport_name": plan.transporter_name,
            "mobile_no": mobile,
            "litres": _decimal(plan.total_litres) if plan.total_litres is not None
            else _decimal(extra.get("total_litres")),
            "total_boxes": _decimal(extra.get("total_boxes")),
            "priority": plan.priority,
            "kanta_weight": _decimal(plan.kanta_weight),
            "invoice_weight": _decimal(plan.invoice_weight),
            "freight": _decimal(plan.freight),
            "total_freight": _decimal(plan.total_freight),
            "remarks": plan.remarks,
            "eway_bill": plan.eway_bill,
        }
