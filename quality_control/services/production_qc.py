# quality_control/services/production_qc.py
"""QA Reports: saving an entry of a report, and approving or sending it back."""

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from ..enums import ParameterType
from ..models import (
    ProductionParameterType,
    ProductionQCEntry,
    ProductionQCResult,
    ProductionQCStatus,
)

class ProductionQCError(Exception):
    """An entry that cannot be saved or decided; the message is for the user."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field

    def as_response_data(self):
        return {self.field: [str(self)]} if self.field else {"detail": str(self)}


def _parse_numeric(value):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _apply_reading(row, value_type, data, user):
    """Put one reading on a result row; save() then judges it against the row's spec."""
    value = (data.get("result_value") or "").strip()
    numeric = data.get("result_numeric")
    if numeric is None and value and value_type in (ParameterType.NUMERIC, ParameterType.RANGE):
        numeric = _parse_numeric(value)
    within_spec = data.get("is_within_spec")
    if value_type == ParameterType.BOOLEAN and value:
        # A pass/fail reading is its own verdict, as on the arrival slip.
        within_spec = value.lower() == "pass"
    row.result_value = value
    row.result_numeric = numeric
    row.is_within_spec = within_spec
    row.remarks = (data.get("remarks") or "").strip()
    row.updated_by = user
    row.save()


def _check_readings(definitions, readings):
    """`definitions`: (parameter_id, name, is_mandatory) for each parameter asked for."""
    unknown = set(readings) - {parameter_id for parameter_id, _, _ in definitions}
    if unknown:
        raise ProductionQCError("A reading was sent for a parameter not in this type.", "results")
    missing = [
        name
        for parameter_id, name, mandatory in definitions
        if mandatory and not (readings.get(parameter_id, {}).get("result_value") or "").strip()
    ]
    if missing:
        raise ProductionQCError(
            f"Enter a value for every mandatory parameter: {', '.join(missing)}.", "results"
        )


def _require_remark_if_out_of_spec(entry):
    if entry.results.filter(is_within_spec=False).exists() and not entry.remarks.strip():
        raise ProductionQCError(
            "A remark is required when any parameter is out of spec.", "remarks"
        )


def _parameters_of(parameter_type):
    parameters = list(parameter_type.parameters.filter(is_active=True).order_by("sequence", "id"))
    if not parameters:
        raise ProductionQCError(
            "This parameter type has no parameters yet. Add them under Parameter Types first.",
            "parameter_type_id",
        )
    return parameters


@transaction.atomic
def create_entry(company, user, *, parameter_type_id, readings, remarks=""):
    """Save a new entry of one of the company's reports and send it for approval.

    A report is not tied to production: there is no line or run to pick.
    """
    parameter_type = ProductionParameterType.objects.filter(
        company=company, pk=parameter_type_id, is_active=True
    ).first()
    if parameter_type is None:
        raise ProductionQCError("Pick a report.", "parameter_type_id")

    parameters = _parameters_of(parameter_type)
    _check_readings(
        [(p.id, p.parameter_name, p.is_mandatory) for p in parameters], readings
    )

    now = timezone.now()
    entry = ProductionQCEntry.objects.create(
        company=company,
        parameter_type=parameter_type,
        checked_at=now,
        status=ProductionQCStatus.PENDING,
        remarks=(remarks or "").strip(),
        submitted_by=user,
        submitted_at=now,
        created_by=user,
        updated_by=user,
    )
    for parameter in parameters:
        row = ProductionQCResult(entry=entry, parameter_master=parameter, created_by=user)
        row.apply_parameter_snapshot(parameter)
        _apply_reading(row, parameter.value_type, readings.get(parameter.id, {}), user)
    _require_remark_if_out_of_spec(entry)
    return entry


@transaction.atomic
def update_entry(entry, user, *, readings, remarks=""):
    """Correct an entry that is waiting for approval or was sent back, and send it again."""
    entry = ProductionQCEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.status == ProductionQCStatus.APPROVED:
        raise ProductionQCError("An approved entry cannot be changed.")

    # A correction is judged against the spec the check was made against: the
    # rows keep their snapshot, and only the readings on them change.
    rows = list(entry.results.order_by("sequence", "id"))
    _check_readings(
        [(r.parameter_master_id, r.parameter_name, r.is_mandatory) for r in rows], readings
    )

    now = timezone.now()
    entry.remarks = (remarks or "").strip()
    entry.status = ProductionQCStatus.PENDING
    entry.submitted_by = user
    entry.submitted_at = now
    entry.updated_by = user
    entry.save()
    for row in rows:
        _apply_reading(row, row.parameter_type, readings.get(row.parameter_master_id, {}), user)
    _require_remark_if_out_of_spec(entry)
    return entry


@transaction.atomic
def approve_entry(entry, user, remarks=""):
    entry = ProductionQCEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.status != ProductionQCStatus.PENDING:
        raise ProductionQCError("Only an entry waiting for approval can be approved.")
    entry.status = ProductionQCStatus.APPROVED
    entry.approved_by = user
    entry.approved_at = timezone.now()
    entry.approval_remarks = (remarks or "").strip()
    entry.updated_by = user
    entry.save()
    return entry


@transaction.atomic
def send_back_entry(entry, user, remarks):
    remarks = (remarks or "").strip()
    if not remarks:
        raise ProductionQCError("Say what needs correcting.", "remarks")
    entry = ProductionQCEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.status != ProductionQCStatus.PENDING:
        raise ProductionQCError("Only an entry waiting for approval can be sent back.")
    entry.status = ProductionQCStatus.SENT_BACK
    entry.sent_back_by = user
    entry.sent_back_at = timezone.now()
    entry.send_back_remarks = remarks
    entry.updated_by = user
    entry.save()
    return entry
