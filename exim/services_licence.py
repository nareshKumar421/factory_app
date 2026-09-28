"""The rules of an export licence. Views and the EXIM copy come through here.

THE ARITHMETIC (EXIM's, unchanged)
 - Each value in USD is its INR value over its exchange rate.
 - The first leg (imports on an Advance Authorisation, exports on a DFIA)
   creates the obligation: its total less ``NORM_ALLOWANCE`` (3.1%).
 - The balance is the obligation less the second leg's total. It goes negative
   when more was exported (Advance) or imported (DFIA) than was owed; EXIM
   allowed that and so does this.

WHAT CHANGED FROM EXIM
EXIM worked the figures out inside each line model's ``save()``, which left
them wrong in three ways: deleting a line changed nothing, an import line on an
Advance licence moved the obligation but not the balance, and editing a line
used the line's OLD quantity. Here every line write, including a delete, ends in
``recompute_totals``, which works all four figures out again from the lines.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import APIException

from .models_licence import Licence, LicenceKind, LicenceLine, LineDirection

#: What the licence allows off the first leg before it becomes the obligation.
#: EXIM's figure, applied the same way to both kinds.
NORM_ALLOWANCE = Decimal("0.031")

_THOUSANDTH = Decimal("0.001")


class EximError(APIException):
    """A rule was broken. Renders as the project's one error shape:

        {"detail": "...", "code": "stable_slug", "context": {...}}
    """

    status_code = 400

    def __init__(self, detail, code, context=None):
        super().__init__({"detail": detail, "code": code, "context": context or {}})


def _q(value) -> Decimal:
    return Decimal(value).quantize(_THOUSANDTH, rounding=ROUND_HALF_UP)


def usd_value(inr, rate) -> Decimal:
    return _q(Decimal(inr) / Decimal(rate))


def figures(kind: str, total_import, total_export) -> tuple[Decimal, Decimal]:
    """(obligation, balance) for a licence of ``kind`` with these leg totals."""
    total_import, total_export = Decimal(total_import), Decimal(total_export)
    first, second = (
        (total_import, total_export) if kind == LicenceKind.ADVANCE else (total_export, total_import)
    )
    obligation = _q(first * (1 - NORM_ALLOWANCE))
    return obligation, _q(obligation - second)


def recompute_totals(licence: Licence, *, user=None) -> Licence:
    """Work the licence's leg totals, obligation and balance out from its lines."""
    sums = dict(
        licence.lines.values_list("direction").annotate(total=Sum("quantity_mts")).order_by()
    )
    licence.total_import_mts = _q(sums.get(LineDirection.IMPORT) or 0)
    licence.total_export_mts = _q(sums.get(LineDirection.EXPORT) or 0)
    licence.obligation_mts, licence.balance_mts = figures(
        licence.kind, licence.total_import_mts, licence.total_export_mts
    )
    fields = ["total_import_mts", "total_export_mts", "obligation_mts", "balance_mts", "updated_at"]
    if user is not None:
        licence.updated_by = user
        fields.append("updated_by")
    licence.save(update_fields=fields)
    return licence


# ---------------------------------------------------------------------------
# Licences
# ---------------------------------------------------------------------------

#: What a person enters on a licence. The USD values and every total are worked out.
LICENCE_FIELDS = (
    "status",
    "issue_date",
    "import_validity",
    "export_validity",
    "cif_value_inr",
    "cif_exchange_rate",
    "fob_value_inr",
    "fob_exchange_rate",
    "authorised_qty_mts",
)


def _apply_usd(licence: Licence) -> None:
    licence.cif_value_usd = usd_value(licence.cif_value_inr, licence.cif_exchange_rate)
    licence.fob_value_usd = usd_value(licence.fob_value_inr, licence.fob_exchange_rate)


def create_licence(*, company, user, kind, number, **data) -> Licence:
    number = number.strip()
    if Licence.objects.filter(company=company, kind=kind, number__iexact=number).exists():
        raise EximError(
            f"{LicenceKind(kind).label} {number} is already on the register.",
            "licence_exists",
            {"number": number},
        )
    licence = Licence(company=company, kind=kind, number=number, created_by=user, updated_by=user)
    for field in LICENCE_FIELDS:
        if field in data:
            setattr(licence, field, data[field])
    _apply_usd(licence)
    licence.obligation_mts, licence.balance_mts = figures(kind, 0, 0)
    licence.save()
    return licence


def update_licence(licence: Licence, *, user, **data) -> Licence:
    """Change what was entered on a licence. Its kind and number never change:
    the number is how the licence is known to customs."""
    for field in LICENCE_FIELDS:
        if field in data:
            setattr(licence, field, data[field])
    _apply_usd(licence)
    licence.updated_by = user
    licence.save()
    return licence


def delete_licence(licence: Licence) -> None:
    licence.delete()


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------

LINE_FIELDS = ("document_no", "document_date", "value_usd", "quantity_mts")


def _check_link(licence: Licence, direction: str, linked_line):
    """A link runs from a second-leg line to a first-leg line of the same licence."""
    if linked_line is None:
        return
    if direction != licence.second_leg:
        raise EximError(
            f"Only {licence.second_leg.lower()} lines are linked on this licence.",
            "link_not_allowed",
            {"direction": direction},
        )
    if linked_line.licence_id != licence.id or linked_line.direction != licence.first_leg:
        raise EximError(
            f"The linked line must be one of this licence's {licence.first_leg.lower()} lines.",
            "link_invalid",
            {"linked_line": linked_line.id},
        )


def _check_date(direction: str, document_date) -> None:
    if direction == LineDirection.IMPORT and document_date is None:
        raise EximError("A bill of entry needs its date.", "date_required", {})


@transaction.atomic
def add_line(licence: Licence, *, user, direction, linked_line=None, **data) -> LicenceLine:
    _check_link(licence, direction, linked_line)
    _check_date(direction, data.get("document_date"))
    line = LicenceLine(
        licence=licence,
        direction=direction,
        linked_line=linked_line,
        created_by=user,
        updated_by=user,
        **{f: data[f] for f in LINE_FIELDS if f in data},
    )
    line.document_no = line.document_no.strip()
    line.save()
    recompute_totals(licence, user=user)
    return line


_UNSET = object()


@transaction.atomic
def update_line(line: LicenceLine, *, user, linked_line=_UNSET, **data) -> LicenceLine:
    """Change a line. Its direction never changes; ``linked_line`` is left as
    it is unless passed (None clears it)."""
    licence = line.licence
    if linked_line is not _UNSET:
        _check_link(licence, line.direction, linked_line)
        line.linked_line = linked_line
    for field in LINE_FIELDS:
        if field in data:
            setattr(line, field, data[field])
    _check_date(line.direction, line.document_date)
    line.document_no = line.document_no.strip()
    line.updated_by = user
    line.save()
    recompute_totals(licence, user=user)
    return line


@transaction.atomic
def delete_line(line: LicenceLine, *, user) -> Licence:
    licence = line.licence
    line.delete()
    return recompute_totals(licence, user=user)
