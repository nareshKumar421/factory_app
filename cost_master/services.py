"""Cost Master services — the single place cost definitions are read/written.

Deliberately company-agnostic: the Cost Master spans the whole factory, so
scope (factory / company / department / value) travels explicitly in the data
rather than being taken from the request's company context.
"""
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
                 department_id=None, value_key=None):
    """The rate in force for a cost type in a context, or None.

    Precedence (most specific wins):
      value+company > value > department+company > department >
      company > factory
    and within a level, the latest ``effective_from <= as_of``.
    """
    as_of = as_of or timezone.localdate()
    candidates = (
        CostRate.objects
        .filter(
            cost_type__code=cost_type_code,
            cost_type__is_active=True,
            is_active=True,
            effective_from__lte=as_of,
        )
        .select_related('cost_type')
    )
    best = None
    for rate in candidates:
        if not _matches_context(rate, company_id, department_id, value_key):
            continue
        rank = (_SPECIFICITY[rate.scope], 1 if rate.company_id else 0,
                rate.effective_from)
        if best is None or rank > best[0]:
            best = (rank, rate)
    return best[1] if best else None


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
