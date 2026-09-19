"""The Dispatch Sheet — the outward register the office has kept in Excel.

For years the dispatch desk has typed a workbook: a tab per company, a block
per day, and a line per invoice that left the gate carrying the truck, the
bilty, the litres and what the freight cost. Every one of those columns is
already in the app — the plan holds the vehicle, transporter, bilty,
priority, kanta weight and freight; the invoice holds the date, the party,
the address and the litres — so the sheet is not a new book to keep. It is
the same rows, laid out the way the desk reads them.

The read spans every company the caller belongs to, and each row says which
it came from: the desk keeps ONE book for the group and turns to Oil,
Beverages or Mart within it, so a read of one company alone would be a
third of the register rather than the whole of it.

Read-only, deliberately. A figure typed here would be a second, disagreeing
copy of something the plan already says; the way to change a line on this
sheet is to change the plan it is a view of.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Dict, Iterable, List

from django.conf import settings
from django.db.models import Max, Q, Sum
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.models import Company
from company.permissions import HasCompanyContext
from gate_core.services.user_scope import user_company_ids, wants_all_companies
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .models import DispatchPlan, DispatchPlanStatus
from .freight_rate_service import FreightRateService
from .permissions import CanViewDispatchSheet
from .serializers import DispatchSheetFilterSerializer
from .services import (
    PIPELINE_STAGE_LABELS,
    DispatchPlansService,
    compute_pipeline_stage,
    pipeline_gate_out_prefetch,
)


#: How long a bilty's freight (or its absence) may be reused. Short, because
#: the money is posted days after the truck leaves and a long memory would keep
#: reporting an empty cell after it landed.
BILTY_FREIGHT_CACHE_TTL_SECONDS = getattr(
    settings, "DISPATCH_SHEET_FREIGHT_CACHE_SECONDS", 15 * 60
)


def _split_by_litres(amount: float, rows: List[Dict[str, Any]]) -> List[float]:
    """Split one truck's freight across the bills it carried.

    By litres, falling back to an even split when nothing on the load has a
    litre figure -- the same rule the app follows when a batch freight is
    typed against a load. The last share takes the rounding, so the parts add
    back to exactly what was posted.
    """
    weights = [float(row.get("litres") or 0) for row in rows]
    total = sum(weights)
    if total <= 0:
        weights = [1.0] * len(rows)
        total = float(len(rows))

    shares: List[float] = []
    running = 0.0
    for index, weight in enumerate(weights):
        if index == len(rows) - 1:
            shares.append(round(amount - running, 2))
        else:
            share = round(amount * weight / total, 2)
            running += share
            shares.append(share)
    return shares


def _decimal(value) -> float | None:
    """A number the sheet can total, or None for a cell that is genuinely empty.

    Zero is a real figure here (a nil-litre line exists on the real sheet, as
    the second bill of a split load), so only None means blank.
    """
    if value in (None, ""):
        return None
    return float(value)


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

        # One reading of where each truck got to, kept: it names the stage AND
        # points at the docking whose weighbridge slip is the kanta weight.
        stages = [compute_pipeline_stage(plan) for plan in plans]
        weighments = self._weighbridge(stages)

        rows = []
        for plan, (stage, gate_out, _) in zip(plans, stages):
            extra = enrichment.get((plan.company_id, plan.sap_invoice_doc_entry)) or {}
            rows.append(self._row(plan, extra, stage, weighments.get(gate_out.vehicle_entry_id if gate_out else None)))

        self._fill_freight_from_sap(plans, rows)

        return Response(
            {
                "data": rows,
                "meta": {
                    "total": len(rows),
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    # A line per company, so the page can label its sheets
                    # before anyone opens one.
                    "counts_by_company": Counter(row["company_code"] for row in rows),
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
        # Imported here: `grpo.models` imports `DispatchPlan`, so naming it at
        # module level would close the circle.
        from grpo.models import GRPOStatus

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
            plans.select_related(
                "company",
                "vehicle",
                "transporter",
                "driver",
                "linked_vehicle_entry",
            )
            # What the carriage actually cost this bill, from the freight GRPO
            # posted against it. Annotated rather than walked, so a month of
            # lines is still one query.
            .annotate(
                posted_freight=Sum(
                    "service_grpo_lines__amount",
                    filter=Q(
                        service_grpo_lines__service_grpo_posting__status=GRPOStatus.POSTED
                    ),
                ),
                posted_freight_rate=Max(
                    "service_grpo_lines__unit_price",
                    filter=Q(
                        service_grpo_lines__service_grpo_posting__status=GRPOStatus.POSTED
                    ),
                ),
            )
            # Where the truck has got to is read off its gate-in and its
            # dockings, so both are prefetched: without them the register would
            # issue two queries per line.
            .prefetch_related(*pipeline_gate_out_prefetch())
            .order_by("dispatch_date", "customer_name", "sap_invoice_doc_num")
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
    def _weighbridge(stages) -> Dict[int, Any]:
        """The loaded weighing for each truck, by the gate entry it belongs to.

        THE LOADED WEIGHT IS NOT ON THE ENTRY THE PLAN LINKS TO.
        A truck visits twice over: it comes in empty and is weighed for its
        tare, then docks, loads, and is weighed again on the way out. Those are
        two different gate entries, and the plan links to the FIRST -- so
        reading its weighment gives a record with a tare, no gross, and a net
        of zero. On the live books that was all 760 of them.

        The load is on the docking's entry. So the weighbridge is fetched by
        the gate-out each row's stage already found, in one query for the whole
        window.
        """
        from weighment.models import Weighment

        entry_ids = {
            gate_out.vehicle_entry_id
            for _, gate_out, _ in stages
            if gate_out is not None and gate_out.vehicle_entry_id
        }
        if not entry_ids:
            return {}
        return {
            weighment.vehicle_entry_id: weighment
            for weighment in Weighment.objects.filter(vehicle_entry_id__in=entry_ids)
        }

    @classmethod
    def _fill_freight_from_sap(cls, plans: List[DispatchPlan], rows: List[Dict[str, Any]]):
        """Freight for the lines the app posted nothing against.

        Most carriage is entered straight into SAP rather than through this
        app, so a bill can be dispatched, paid for and still show an empty
        Freight column -- which is the column the desk reads this page for.
        SAP files it under the bilty, and so does the register, so the two can
        be put together.

        The money is the TRUCK'S: one bilty carries several bills. It is split
        across them by litres, the same rule the app uses when somebody types a
        batch freight against a load, so the column adds up to what the window
        actually cost rather than to the same lorry counted five times.

        The rate column is left alone. SAP has the money, not the price per
        litre it was struck at, and a rate worked backwards from an allocation
        would be a number nobody agreed.
        """
        wanted: Dict[int, set] = {}
        for plan, row in zip(plans, rows):
            if row["total_freight"] is None and row["bilty_no"].strip():
                wanted.setdefault(plan.company_id, set()).add(row["bilty_no"].strip())
        if not wanted:
            return

        codes = {plan.company_id: plan.company.code for plan in plans}
        by_company: Dict[int, Dict[str, float]] = {}
        for company_id, bilties in wanted.items():
            try:
                context = CompanyContext(codes[company_id])
                service = FreightRateService(context, company_code=codes[company_id])
                by_company[company_id] = cls._cached_bilty_freight(service, bilties)
            except (SAPConnectionError, SAPDataError):
                # The register is readable without it; the cells stay empty.
                by_company[company_id] = {}

        # One bilty, one truck, however many bills rode on it.
        loads: Dict[tuple, List[Dict[str, Any]]] = {}
        for plan, row in zip(plans, rows):
            bilty = row["bilty_no"].strip()
            if row["total_freight"] is None and bilty:
                loads.setdefault((plan.company_id, bilty), []).append(row)

        for (company_id, bilty), load in loads.items():
            amount = by_company.get(company_id, {}).get(bilty)
            if not amount:
                continue
            for row, share in zip(load, _split_by_litres(amount, load)):
                row["total_freight"] = share
                row["freight_from_sap"] = True

    @staticmethod
    def _cached_bilty_freight(service, bilties: set) -> Dict[str, float]:
        """Remembered briefly, not for hours.

        A dispatched bill's freight arrives days after the truck left, so a
        long memory would keep saying "nothing yet" well after the money was
        posted. Short enough that the page is not asking on every refresh,
        short enough that today's posting shows up the same morning.
        """
        prefix = f"dispatch_sheet:freight:{service.schema}:"
        cached = cache.get_many([f"{prefix}{b}" for b in bilties])
        found = {key[len(prefix):]: value for key, value in cached.items()}

        missing = sorted(bilties - set(found))
        if not missing:
            return {k: v for k, v in found.items() if v is not None}

        fetched = service.freight_by_bilty(missing)
        # Misses are remembered too, as None: a bilty whose freight has not
        # been posted is most of them, and re-asking SAP about every one of
        # those on every refresh is the cost this cache exists to avoid.
        cache.set_many(
            {f"{prefix}{b}": fetched.get(b) for b in missing},
            BILTY_FREIGHT_CACHE_TTL_SECONDS,
        )
        found.update(fetched)
        return {k: v for k, v in found.items() if v is not None}

    @staticmethod
    def _row(plan: DispatchPlan, extra: Dict[str, Any], stage: str, weighment) -> Dict[str, Any]:
        """One line of the register, in the workbook's own vocabulary.

        Where the plan and SAP both hold a column, the plan wins: the desk
        edits the plan, and a correction typed there is the whole point of it
        being editable. SAP fills the cell only when the plan's is empty.
        """
        mobile = plan.mobile_no or plan.driver_mobile_no

        # The weighbridge's own figure for the loaded truck. Its NET is the
        # load -- gross off the bridge less the tare taken when the truck came
        # in empty -- and it is only a figure at all once both weighings have
        # happened, which `Weighment` says by leaving net at zero until then.
        kanta = plan.kanta_weight
        if kanta is None and weighment is not None and weighment.net_weight:
            kanta = weighment.net_weight
        return {
            "plan_id": plan.id,
            "sap_invoice_doc_entry": plan.sap_invoice_doc_entry,
            "company_code": plan.company.code,
            "company_name": plan.company.name,
            "booking_status": plan.booking_status,
            # Where the truck itself has got to -- booked, at the gate,
            # docked, gone. The same reading the pipeline board makes, so a
            # line here and a card there never disagree.
            "vehicle_stage": stage,
            "vehicle_stage_label": PIPELINE_STAGE_LABELS.get(stage, stage),
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
            "kanta_weight": _decimal(kanta),
            "invoice_weight": _decimal(plan.invoice_weight),
            # What the desk typed wins; failing that, what was actually posted
            # as this bill's carriage.
            "freight": _decimal(
                plan.freight if plan.freight is not None else plan.posted_freight_rate
            ),
            "total_freight": _decimal(
                plan.total_freight
                if plan.total_freight is not None
                else plan.posted_freight
            ),
            "remarks": plan.remarks,
            "eway_bill": plan.eway_bill,
            # Set when the figure came from SAP rather than from the app, so
            # the page can say where a number it did not record came from.
            "freight_from_sap": False,
        }
