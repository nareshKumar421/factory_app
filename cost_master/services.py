"""Cost Master services — the single place cost definitions are read/written.

Deliberately company-agnostic: the Cost Master spans the whole factory, so
scope (factory / company / department / value) travels explicitly in the data
rather than being taken from the request's company context.
"""
from django.db.models import Q
from django.utils import timezone

from company.models import Company
from accounts.models import Department
from .models import CostType, CostRate, CostScope

# Most specific wins. Within a scope, a company-specific row beats the
# company-agnostic variant; within a (scope, company) pair the latest
# effective_from <= as_of wins.
_SPECIFICITY = {
    CostScope.FACTORY: 0,
    CostScope.COMPANY: 1,
    CostScope.DEPARTMENT: 2,
    CostScope.VALUE: 3,
}


# ======================================================================
# Cost types
# ======================================================================
def list_cost_types(include_inactive=False):
    qs = CostType.objects.all()
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs


def create_cost_type(data: dict, user=None) -> CostType:
    if CostType.objects.filter(code=data['code']).exists():
        raise ValueError(f"A cost type with code '{data['code']}' already exists.")
    return CostType.objects.create(
        code=data['code'],
        name=data['name'],
        description=data.get('description', ''),
        default_basis=data.get('default_basis') or CostType._meta.get_field('default_basis').default,
        is_credit=data.get('is_credit', False),
        created_by=user,
    )


def update_cost_type(cost_type_id: int, data: dict, user=None) -> CostType:
    cost_type = _get_cost_type_or_raise(cost_type_id)
    for field in ['name', 'description', 'default_basis', 'is_credit', 'is_active']:
        if field in data:
            setattr(cost_type, field, data[field])
    cost_type.updated_by = user
    cost_type.save()
    return cost_type


def delete_cost_type(cost_type_id: int) -> None:
    """Soft-delete. The code stays reserved (unique across inactive rows too)
    so a resolved historical rate never changes meaning."""
    cost_type = _get_cost_type_or_raise(cost_type_id)
    cost_type.is_active = False
    cost_type.save(update_fields=['is_active', 'updated_at'])


def _get_cost_type_or_raise(cost_type_id) -> CostType:
    try:
        return CostType.objects.get(id=cost_type_id)
    except CostType.DoesNotExist:
        raise ValueError("Cost type not found.")


# ======================================================================
# Rates
# ======================================================================
def list_rates(cost_type_id=None, scope=None, company_id=None, department_id=None,
               value_key=None, as_of=None, history=False):
    """Rates **in force on ``as_of``** (today by default), one per
    (cost_type, scope target). ``history=True`` returns every dated row
    instead, newest first — the audit trail.
    """
    qs = (
        CostRate.objects
        .filter(is_active=True, cost_type__is_active=True)
        .select_related('cost_type', 'company', 'department')
    )
    if cost_type_id:
        qs = qs.filter(cost_type_id=cost_type_id)
    if scope:
        qs = qs.filter(scope=scope)
    if company_id is not None:
        qs = qs.filter(company_id=company_id)
    elif scope in (CostScope.DEPARTMENT, CostScope.VALUE):
        # No company given → the company-agnostic rows for the scope.
        qs = qs.filter(company__isnull=True)
    if department_id is not None:
        qs = qs.filter(department_id=department_id)
    if value_key:
        qs = qs.filter(value_key=value_key)
    if history:
        return qs.order_by('cost_type_id', 'scope', '-effective_from')
    as_of = as_of or timezone.localdate()
    current = {}
    for r in qs.filter(effective_from__lte=as_of).order_by('effective_from', 'id'):
        current[(r.cost_type_id, r.scope, r.company_id, r.department_id, r.value_key)] = r
    return sorted(
        current.values(),
        key=lambda r: (r.cost_type_id, _SPECIFICITY[r.scope], r.company_id or 0),
    )


def upsert_rate(data: dict, user=None) -> CostRate:
    """Set the rate for (cost_type, scope target) **from a date**.

    A new ``effective_from`` creates a NEW row and leaves the superseded one in
    place. Re-posting the same date corrects that day's row (a typo fix), the
    only case where a stored rate is overwritten.
    """
    cost_type = _get_cost_type_or_raise(data['cost_type_id'])
    scope = data['scope']
    company_id, department_id, value_key = _validated_scope_target(
        scope,
        company_id=data.get('company_id'),
        department_id=data.get('department_id'),
        value_key=(data.get('value_key') or '').strip(),
    )
    effective_from = data.get('effective_from') or timezone.localdate()
    rate, _ = CostRate.objects.update_or_create(
        cost_type=cost_type,
        scope=scope,
        company_id=company_id,
        department_id=department_id,
        value_key=value_key,
        effective_from=effective_from,
        is_active=True,
        defaults={
            'basis': data.get('basis') or cost_type.default_basis,
            'rate': data['rate'],
            'notes': data.get('notes', ''),
            'updated_by': user,
        },
    )
    return rate


def delete_rate(rate_id: int) -> None:
    """Soft-delete (is_active=False) so the partial unique constraint frees up
    and a later upsert re-creates cleanly."""
    try:
        rate = CostRate.objects.get(id=rate_id)
    except CostRate.DoesNotExist:
        raise ValueError("Cost rate not found.")
    rate.is_active = False
    rate.save(update_fields=['is_active', 'updated_at'])


def _validated_scope_target(scope, company_id=None, department_id=None, value_key=''):
    """Normalize + validate the scope's target fields; irrelevant ones are
    dropped so the unique constraints see canonical rows."""
    if scope not in CostScope.values:
        raise ValueError(f"Unknown scope '{scope}'.")
    if scope == CostScope.FACTORY:
        return None, None, ''
    if company_id is not None and not Company.objects.filter(id=company_id).exists():
        raise ValueError("Company not found.")
    if scope == CostScope.COMPANY:
        if company_id is None:
            raise ValueError("A company-wide rate needs a company.")
        return company_id, None, ''
    if scope == CostScope.DEPARTMENT:
        if department_id is None:
            raise ValueError("A department-wide rate needs a department.")
        if not Department.objects.filter(id=department_id).exists():
            raise ValueError("Department not found.")
        return company_id, department_id, ''
    # VALUE
    if not value_key:
        raise ValueError("A value-specific rate needs a value key.")
    return company_id, None, value_key


# ======================================================================
# Resolution — for cost engines that consume the master
# ======================================================================
def resolve_rate(cost_type_code: str, as_of=None, company_id=None,
                 department_id=None, value_key=None, fallback_earliest=False):
    """The rate in force for a cost type in a context, or None.

    Precedence (most specific wins):
      value+company > value > department+company > department >
      company > factory
    and within a level, the latest ``effective_from <= as_of``. The single-code
    adapter over :func:`resolve_rates_bulk`, so precedence lives in one place.
    """
    return resolve_rates_bulk(
        {'k': cost_type_code}, as_of=as_of, company_id=company_id,
        department_id=department_id, value_key=value_key,
        fallback_earliest=fallback_earliest,
    )['k']


def resolve_rates_bulk(code_map, as_of=None, company_id=None, department_id=None,
                       value_key=None, fallback_earliest=False):
    """Resolve many cost types in one query: ``{key: CostRate or None}``.

    ``code_map`` is ``{consumer_key: cost_type_code}`` — cost engines keep
    their own category keys and map them to central codes here.

    ``fallback_earliest=True`` covers dates that predate every known rate (runs
    older than the one-time import, whose rows carry their source's creation
    date): the earliest already-effective rate applies instead of silently
    costing at zero. Rates scheduled for the FUTURE (``effective_from`` after
    today) stay dormant either way — entering next month's rate ahead of time
    must never reprice anything before it starts.
    """
    as_of = as_of or timezone.localdate()
    today = timezone.localdate()
    # The context filters mirror _matches_context, pushed into SQL so a resolve
    # never ships another company's/subject's rate history over the wire.
    qs = (
        CostRate.objects
        .filter(cost_type__code__in=set(code_map.values()),
                cost_type__is_active=True, is_active=True,
                effective_from__lte=max(as_of, today) if fallback_earliest else as_of)
        .filter(Q(company__isnull=True) | Q(company_id=company_id))
        .filter(Q(department__isnull=True) | Q(department_id=department_id))
        .filter(Q(value_key='') | Q(value_key=value_key or ''))
        .select_related('cost_type')
    )
    best = {}
    fallback = {}
    for rate in qs:
        if not _matches_context(rate, company_id, department_id, value_key):
            continue
        code = rate.cost_type.code
        if rate.effective_from <= as_of:
            # Most specific wins; within a level the latest in-force date.
            rank = (_SPECIFICITY[rate.scope], 1 if rate.company_id else 0,
                    rate.effective_from)
            if code not in best or rank > best[code][0]:
                best[code] = (rank, rate)
        elif fallback_earliest:
            # Same precedence, but the EARLIEST date within a level — the
            # closest known rate after a date nothing was in force on.
            rank = (_SPECIFICITY[rate.scope], 1 if rate.company_id else 0,
                    -rate.effective_from.toordinal())
            if code not in fallback or rank > fallback[code][0]:
                fallback[code] = (rank, rate)
    result = {}
    for key, code in code_map.items():
        picked = best.get(code) or fallback.get(code)
        result[key] = picked[1] if picked else None
    return result


def resolve_amount(cost_type_code, default=None, as_of=None, company_id=None,
                   department_id=None, value_key=None, fallback_earliest=True):
    """The in-force rate value for a cost type, or ``default`` when the master
    has no row — the one-liner for consumers that just need a number."""
    resolved = resolve_rate(
        cost_type_code, as_of=as_of, company_id=company_id,
        department_id=department_id, value_key=value_key,
        fallback_earliest=fallback_earliest,
    )
    return resolved.rate if resolved else default


def _matches_context(rate, company_id, department_id, value_key):
    if rate.scope == CostScope.FACTORY:
        return True
    if rate.scope == CostScope.COMPANY:
        return company_id is not None and rate.company_id == company_id
    if rate.company_id is not None and rate.company_id != company_id:
        return False
    if rate.scope == CostScope.DEPARTMENT:
        return department_id is not None and rate.department_id == department_id
    return bool(value_key) and rate.value_key == value_key
