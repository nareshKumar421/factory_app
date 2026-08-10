"""The daily operating loop — the playbook's six lines of arithmetic.

    1  units per day     = remaining plan qty / working days left
    2  material per day  = units per day x BOM quantity per unit
    3  free stock        = godown stock - committed
    4  days of cover     = free stock / material per day
    5  lead time         = Nth percentile of the last N actual deliveries
    6  verdict           = days of cover < lead time  ->  RED

Every intermediate number is stored on the row, not just the answer, because the
whole point of this method is that a person can redo it on paper and disagree.

Two things are deliberate and worth knowing:

* **Cover is converted from production days to calendar days** before it is
  compared with a lead time. Cover is consumed only on working days; a supplier's
  40 days run over weekends too. Comparing them raw overstates how long the stock
  lasts.
* **Nothing is silently skipped.** A material with no BOM, no stock record or no
  delivery history becomes an UNKNOWN row plus a data-quality issue. A material
  the system cannot judge is a finding, not an absence — dropping it is exactly
  how a real shortage hides.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import (
    CoverVerdict,
    DailyRun,
    DailyRunRow,
    DataQualityIssue,
    MaterialLeadTime,
    MonitoredSku,
    MaterialStock,
    OperatingParameters,
    RunStatus,
    SupplierDelivery,
)

ZERO = Decimal("0")


def _q(value, places="0.01"):
    return Decimal(value).quantize(Decimal(places))


def percentile(values, pct):
    """The ``pct``-th percentile of ``values`` (nearest-rank).

    Nearest-rank rather than interpolation: these are whole days from real
    deliveries, and inventing a 38.4-day delivery that never happened would make
    the number harder to defend, not more accurate.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, min(len(ordered), -(-len(ordered) * int(pct) // 100)))
    return ordered[rank - 1]


def measured_lead_time(company_code, material_code, params):
    """``(days, sample_count)`` measured from real deliveries, or ``(None, n)``.

    Returns None when there are too few samples to trust rather than averaging
    two deliveries into a confident-looking number.
    """
    samples = [
        d.days_taken
        for d in SupplierDelivery.objects.filter(
            company_code=company_code, material_code=material_code
        )
        if d.days_taken >= 0
    ]
    if len(samples) < params.min_delivery_samples:
        return None, len(samples)
    return percentile(samples, params.lead_time_percentile), len(samples)


def _lead_time_for(company_code, material_code, params, template_lead_times):
    """``(days, source, samples)``.

    Measured history wins: the playbook is explicit that lead time comes from what
    the supplier actually did, "not from memory". The template's typed-in figure is
    the fallback so a material with no history is still judged rather than dropped.
    """
    days, samples = measured_lead_time(company_code, material_code, params)
    if days is not None:
        return days, "MEASURED", samples
    template = template_lead_times.get(material_code)
    if template is not None and template.lead_time_days:
        return template.lead_time_days, "TEMPLATE", samples
    return None, "", samples


def _verdict(cover_calendar, lead_days, params):
    if lead_days is None:
        return CoverVerdict.UNKNOWN
    if cover_calendar < Decimal(lead_days):
        return CoverVerdict.RED
    if cover_calendar < Decimal(lead_days) * Decimal(params.amber_multiplier):
        return CoverVerdict.AMBER
    return CoverVerdict.GREEN


@transaction.atomic
def build_daily_run(company_code, run_date=None, *, replace=True):
    """Build (or rebuild) the run for one day. Returns the :class:`DailyRun`.

    Rebuilding the same day replaces the previous attempt: a morning's run is a
    snapshot of that morning, and two versions of it would make the verdict log
    ambiguous.
    """
    run_date = run_date or timezone.localdate()
    params = OperatingParameters.for_company(company_code)

    existing = DailyRun.objects.filter(
        company_code=company_code, run_date=run_date
    ).first()
    if existing and not replace:
        return existing
    if existing:
        existing.delete()

    run = DailyRun.objects.create(
        company_code=company_code,
        run_date=run_date,
        status=RunStatus.GENERATED,
        parameters_snapshot={
            "lead_time_percentile": params.lead_time_percentile,
            "min_delivery_samples": params.min_delivery_samples,
            "amber_multiplier": str(params.amber_multiplier),
            "working_days_per_month": str(params.working_days_per_month),
            "calendar_days_per_month": str(params.calendar_days_per_month),
            "max_red_before_block": params.max_red_before_block,
        },
    )

    template_lead_times = {
        lt.material_code: lt
        for lt in MaterialLeadTime.objects.filter(
            company_code=company_code, is_active=True
        )
    }
    stock_by_material = {
        s.material_code: s
        for s in MaterialStock.objects.filter(company_code=company_code)
    }

    issues, rows = [], []
    skus = MonitoredSku.objects.filter(
        company_code=company_code, is_active=True
    ).prefetch_related("components")
    if not skus:
        issues.append(DataQualityIssue(
            run=run, code="NO_SKU", message=(
                "No finished goods are being monitored, so nothing was checked."
            ), blocking=True,
        ))

    for sku in skus:
        if not sku.working_days_left:
            issues.append(DataQualityIssue(
                run=run, code="NO_WORKING_DAYS", sku_code=sku.sku_code, blocking=True,
                message=(
                    f"{sku.sku_code} has no working days left on the plan, so a daily "
                    "consumption rate cannot be worked out."
                ),
            ))
            continue
        components = list(sku.components.all())
        if not components:
            issues.append(DataQualityIssue(
                run=run, code="NO_BOM", sku_code=sku.sku_code, blocking=True,
                message=f"{sku.sku_code} has no recipe on file — none of its materials were checked.",
            ))
            continue

        units_per_day = sku.units_per_day
        for component in components:
            per_day = units_per_day * Decimal(component.quantity_per_unit)
            stock = stock_by_material.get(component.material_code)

            if stock is None:
                issues.append(DataQualityIssue(
                    run=run, code="NO_STOCK", sku_code=sku.sku_code,
                    item_code=component.material_code, blocking=True,
                    message=(
                        f"No stock figure for {component.material_code} — it cannot be "
                        "judged, and may be short without anyone seeing it."
                    ),
                ))
            free = stock.free if stock else ZERO
            on_hand = Decimal(stock.on_hand) if stock else ZERO
            committed = Decimal(stock.committed) if stock else ZERO

            cover = free / per_day if per_day > 0 else ZERO
            cover_calendar = params.cover_days_to_calendar(cover)

            lead_days, source, samples = _lead_time_for(
                company_code, component.material_code, params, template_lead_times
            )
            if lead_days is None:
                issues.append(DataQualityIssue(
                    run=run, code="NO_LEAD_TIME", sku_code=sku.sku_code,
                    item_code=component.material_code, blocking=True,
                    message=(
                        f"No lead time for {component.material_code}: only {samples} past "
                        f"delivery(ies) on file and nothing in the reference template."
                    ),
                ))
            elif source == "TEMPLATE":
                issues.append(DataQualityIssue(
                    run=run, code="LEAD_TIME_FROM_TEMPLATE", sku_code=sku.sku_code,
                    item_code=component.material_code, blocking=False,
                    message=(
                        f"{component.material_code} is using the typed-in lead time "
                        f"({lead_days}d) — only {samples} past delivery(ies) on file."
                    ),
                ))

            if stock is not None and per_day > 0 and free <= 0:
                issues.append(DataQualityIssue(
                    run=run, code="ZERO_FREE_STOCK", sku_code=sku.sku_code,
                    item_code=component.material_code, blocking=False,
                    message=(
                        f"{component.material_code} has no free stock "
                        f"({on_hand} on hand, {committed} committed)."
                    ),
                ))

            verdict = _verdict(cover_calendar, lead_days, params)

            stockout = order_by = None
            days_late = 0
            if lead_days is not None:
                stockout = run_date + timedelta(days=int(cover_calendar))
                order_by = stockout - timedelta(days=int(lead_days))
                if order_by < run_date:
                    days_late = (run_date - order_by).days

            rows.append(DailyRunRow(
                run=run,
                sku_code=sku.sku_code,
                material_code=component.material_code,
                material_name=component.material_name,
                material_type=component.material_type,
                supplier_name=(
                    template_lead_times[component.material_code].supplier_name
                    if component.material_code in template_lead_times else ""
                ),
                unit=component.unit,
                units_per_day=_q(units_per_day, "0.000001"),
                quantity_per_unit=component.quantity_per_unit,
                consumption_per_day=_q(per_day, "0.000001"),
                on_hand=on_hand,
                committed=committed,
                free_stock=free,
                days_of_cover=_q(cover),
                cover_calendar_days=_q(cover_calendar),
                lead_time_days=lead_days,
                lead_time_source=source,
                lead_time_samples=samples,
                stockout_date=stockout,
                order_by_date=order_by,
                days_late=days_late,
                verdict=verdict,
            ))

    DailyRunRow.objects.bulk_create(rows)
    DataQualityIssue.objects.bulk_create(issues)

    counts = {v: 0 for v in CoverVerdict}
    for row in rows:
        counts[row.verdict] += 1
    run.red_count = counts[CoverVerdict.RED]
    run.amber_count = counts[CoverVerdict.AMBER]
    run.green_count = counts[CoverVerdict.GREEN]
    run.unknown_count = counts[CoverVerdict.UNKNOWN]
    run.issue_count = len(issues)
    # A flood of reds means the inputs are wrong, not that the factory is on fire.
    # Marking it here means nobody has to remember the rule at 08:00.
    if not run.is_credible:
        run.status = RunStatus.BLOCKED
    run.save(update_fields=[
        "red_count", "amber_count", "green_count", "unknown_count",
        "issue_count", "status",
    ])
    return run
