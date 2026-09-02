"""
factory_expense/rates.py

Reading the factory's rates out of the Cost Master.

``cost_master`` is the single catalog of what things cost, and this board is one
of its consumers. Everything here is a read; the board never writes a rate.

Why not simply call ``cost_master.services.list_rates``? Its company filter is
*exclusive* — pass a company and you get only that company's rows, omit it and
you get only the company-agnostic ones — which is right for a rate-management
screen listing one scope at a time. Costing a gate entry needs the opposite: a
company-specific row where one exists, silently falling back to the
company-agnostic row and then to the factory-wide row where it does not. That
fallback chain is what these helpers add; the precedence order itself is
``cost_master``'s, mirrored in :data:`SCOPE_SPECIFICITY`.
"""

from decimal import Decimal

from django.db.models import Q

from cost_master.models import CostRate, CostType

from .constants import SCOPE_SPECIFICITY

ZERO = Decimal("0.00")


def load_rates(code: str, company, on_date):
    """Every Cost Master row for ``code`` that could apply to this company today.

    One query. Rows are filtered to the scopes and companies that can possibly
    win, then ranked in Python — a fortnight of gate entries would otherwise be
    a query each.
    """
    return load_rates_by_company(code, [company], on_date).get(company.id, [])


def load_rates_by_company(code: str, companies, on_date):
    """The same, for several companies at once: ``{company_id: [rows]}``.

    Still one query no matter how many companies. A company-agnostic row (one
    with no company set) belongs to every company's list, because that is
    exactly what "applies everywhere" means — so the lists overlap, and a
    caller must resolve against the list for the company that owns the thing
    being priced rather than against a merged pile. Priced the other way, one
    company's ₹700 rate would leak onto another company's labourers.
    """
    cost_type = CostType.objects.filter(code=code, is_active=True).first()
    if cost_type is None:
        return {}

    rows = list(
        CostRate.objects.filter(
            cost_type=cost_type,
            is_active=True,
            effective_from__lte=on_date,
        )
        .filter(Q(company__isnull=True) | Q(company__in=companies))
        .select_related("department", "company")
    )

    shared = [row for row in rows if row.company_id is None]
    by_company = {company.id: list(shared) for company in companies}
    for row in rows:
        if row.company_id is not None and row.company_id in by_company:
            by_company[row.company_id].append(row)
    return by_company


def _rank(rate, department_id):
    """How hard a row competes. Higher wins; None means it does not apply."""
    scope = rate.scope
    if scope == "FACTORY":
        pass
    elif scope == "COMPANY":
        if rate.company_id is None:
            return None
    elif scope == "DEPARTMENT":
        if department_id is None or rate.department_id != department_id:
            return None
    else:
        # VALUE rows are keyed to something this board does not identify.
        return None

    return (
        SCOPE_SPECIFICITY[scope],
        1 if rate.company_id is not None else 0,
        rate.effective_from,
        rate.id,
    )


def resolve(rates, department_id, on_date):
    """The one row that prices a thing on ``on_date``, or None.

    ``rates`` comes from :func:`load_rates`, so it is already limited to rows
    effective on or before the board's date for the right company.
    """
    best = None
    best_rank = None
    for rate in rates:
        if rate.effective_from > on_date:
            continue
        rank = _rank(rate, department_id)
        if rank is None:
            continue
        if best_rank is None or rank > best_rank:
            best, best_rank = rate, rank
    return best


def monthly_amounts_by_department(rates, on_date):
    """Per-department monthly figures for a ``PER_MONTH`` cost type.

    Returns ``[(department_id, department_name, amount), …]``.

    When any department-scoped row is in force, only those are used: they are
    the detailed breakdown, and adding a company-wide blanket on top would
    count the same salary bill twice. With no department rows at all, the
    blanket row is returned once as a single unallocated line.
    """
    department_rows = {}
    blanket = None
    blanket_rank = None

    for rate in rates:
        if rate.effective_from > on_date:
            continue
        if rate.scope == "DEPARTMENT" and rate.department_id:
            rank = (
                1 if rate.company_id is not None else 0,
                rate.effective_from,
                rate.id,
            )
            current = department_rows.get(rate.department_id)
            if current is None or rank > current[0]:
                department_rows[rate.department_id] = (rank, rate)
        elif rate.scope in ("FACTORY", "COMPANY"):
            rank = (
                SCOPE_SPECIFICITY[rate.scope],
                1 if rate.company_id is not None else 0,
                rate.effective_from,
                rate.id,
            )
            if blanket_rank is None or rank > blanket_rank:
                blanket, blanket_rank = rate, rank

    if department_rows:
        return [
            (rate.department_id, rate.department.name, Decimal(rate.rate))
            for _, rate in sorted(
                department_rows.values(),
                key=lambda pair: pair[1].department.name,
            )
        ]

    if blanket is not None:
        return [(None, "All departments", Decimal(blanket.rate))]

    return []
