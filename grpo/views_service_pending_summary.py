"""
grpo/views_service_pending_summary.py

How many service GRPOs are pending, by how long they have waited.

This exists because the queue endpoint beside it cannot answer that question,
for two reasons that are both deliberate there and both wrong for a wall board:

  1. **It is paginated.** `GET service/pending/` answers 25 rows by default and
     the true size only in `count`. Anything counting the returned rows counts
     the page, and a backlog of three hundred reads as twenty-five.
  2. **It is scoped to one month.** DISPATCHED plans are filtered to the current
     month so a bare call cannot drag the whole history through a per-row SAP
     bill-header snapshot. That bound is right for a table of rows. It is fatal
     for age bands: on the 12th of a month the oldest a dispatched plan can be
     is 11 days, so a "45 days and older" band is structurally always zero and
     reads as a queue nobody is neglecting.

The escape is that the SAP cost is per ROW, not per plan. The plan set itself is
one Postgres query, so an aggregate over the whole backlog costs no SAP calls at
all — which is why this endpoint can afford to drop the month bound that the
row-serving one cannot.

Read-only, no SAP, no pagination, no month.
"""

import logging

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from dispatch_plans.models import DispatchPlanStatus
from dispatch_plans.permissions import CanViewBiltyServiceGRPOQueue

from .services import GRPOService

logger = logging.getLogger(__name__)

# Cumulative floors, in days since dispatch. Mirrors the control board's bands.
AGE_BANDS = (15, 30, 45)


class ServicePendingSummaryAPI(APIView):
    """
    GET /api/v1/grpo/service/pending/summary/

    The whole pending queue counted, never a page of it, and never one month.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBiltyServiceGRPOQueue]

    def get(self, request):
        company_code = request.company.company.code
        service = GRPOService(company_code=company_code)
        today = timezone.localdate()

        # The whole backlog, grouped by bilty exactly as the queue table groups
        # it: one bilty covering several invoices is ONE pending GRPO, and
        # counting plans instead of groups would overstate the queue by the
        # multi-invoice consignments.
        plans = service.get_pending_service_grpo_entries(all_months=True)

        # `unpriced` is per bucket as well as per column. Freight is typed in
        # after the fact, so a whole band of fresh bilties routinely carries no
        # amount at all -- and a bucket reporting 0.0 with no way to say why
        # renders on the board as "nothing owed on 170 bilties".
        buckets = {
            band: {"band": band, "documents": 0, "amount": 0.0, "unpriced": 0}
            for band in (0,) + AGE_BANDS
        }
        booked = dispatched = ready = 0
        undated = 0
        unpriced = 0
        oldest_days = None
        total_amount = 0.0

        for plan in plans:
            if plan.booking_status == DispatchPlanStatus.BOOKED:
                booked += 1
            else:
                dispatched += 1
            if service.service_grpo_stage(plan) == service.STAGE_READY:
                ready += 1

            # Freight is not a blocker — the operator types the amount on the
            # post form — so a plan without one is counted and left unpriced
            # rather than dropped or priced at nothing.
            amount = service._line_amount_from_plan(plan)
            amount = float(amount or 0)
            if amount > 0:
                total_amount += amount
            else:
                unpriced += 1

            if plan.dispatch_date is None:
                # No dispatch date is no age. Counted in the total, in no band —
                # the same rule the board's counted columns follow.
                undated += 1
                continue

            age = (today - plan.dispatch_date).days
            oldest_days = age if oldest_days is None else max(oldest_days, age)

            band = 0
            for floor in AGE_BANDS:
                if age >= floor:
                    band = floor
            buckets[band]["documents"] += 1
            if amount > 0:
                buckets[band]["amount"] += amount
            else:
                buckets[band]["unpriced"] += 1

        return Response(
            {
                "company_code": company_code,
                "documents": len(plans),
                "amount": total_amount,
                "unpriced": unpriced,
                "undated": undated,
                "booked": booked,
                "dispatched": dispatched,
                "ready": ready,
                "oldest_days": oldest_days,
                # Exclusive buckets that sum to the dated total; the caller
                # accumulates them into whatever floors it draws.
                "buckets": [buckets[band] for band in (0,) + AGE_BANDS],
            }
        )
