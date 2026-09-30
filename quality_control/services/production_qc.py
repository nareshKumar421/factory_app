# quality_control/services/production_qc.py
"""Production QC: which lines can be checked, and saving / deciding a check."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Max, OuterRef, Subquery
from django.utils import timezone

from ..enums import ParameterType
from ..models import (
    ProductionParameterType,
    ProductionQCEntry,
    ProductionQCResult,
    ProductionQCStatus,
)

# A line counts as running while its run has an open segment, and for this long
# after its latest segment started even if the line is stopped just now — a
# breakdown or the lunch stop closes the segment, and QC still checks the line.
# Older IN_PROGRESS runs are runs nobody completed, not lines that are running.
RUNNING_WINDOW = timedelta(hours=24)


class ProductionQCError(Exception):
    """A check that cannot be saved or decided; the message is for the user."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field

    def as_response_data(self):
        return {self.field: [str(self)]} if self.field else {"detail": str(self)}


@dataclass
class RunningLine:
    line_id: int
    line_name: str
    run_id: int
    run_number: int
    run_date: object
    item_code: str
    product: str
    is_running_now: bool
    last_started_at: object
    stopped_at: object


def running_lines(company, now=None):
    """The lines a check can be made on, one run per line.

    For each line, the IN_PROGRESS run whose latest segment started most
    recently — so a stale run nobody completed never shadows today's run.
    """
    from production_execution.models import ProductionRun, ProductionSegment, RunStatus

    now = now or timezone.now()
    latest_segment = ProductionSegment.objects.filter(production_run=OuterRef("pk")).order_by(
        "-start_time"
    )
    runs = (
        ProductionRun.objects.filter(company=company, status=RunStatus.IN_PROGRESS)
        .annotate(
            last_started_at=Max("segments__start_time"),
            last_segment_open=Subquery(latest_segment.values("is_active")[:1]),
            last_segment_end=Subquery(latest_segment.values("end_time")[:1]),
        )
        .filter(last_started_at__isnull=False)
        .select_related("line")
        .order_by("line_id", "-last_started_at", "-id")
    )

    lines = {}
    for run in runs:
        if run.line_id in lines:
            continue
        open_now = bool(run.last_segment_open) and run.last_segment_end is None
        if not open_now and run.last_started_at < now - RUNNING_WINDOW:
            continue
        lines[run.line_id] = RunningLine(
            line_id=run.line_id,
            line_name=run.line.name,
            run_id=run.id,
            run_number=run.run_number,
            run_date=run.date,
            item_code=(run.item_code or "").strip().upper(),
            product=run.product,
            is_running_now=open_now,
            last_started_at=run.last_started_at,
            stopped_at=None if open_now else run.last_segment_end,
        )
    return sorted(lines.values(), key=lambda line: line.line_name.lower())


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
def create_entry(company, user, *, run_id, parameter_type_id, readings, remarks=""):
    """Save a new check on a running line and send it for approval.

    Any active type of the company can be checked on any line: types are not
    tied to products.
    """
    line = next((l for l in running_lines(company) if l.run_id == run_id), None)
    if line is None:
        raise ProductionQCError(
            "That run is not on a running line any more. Pick the line again.", "run_id"
        )
    parameter_type = ProductionParameterType.objects.filter(
        company=company, pk=parameter_type_id, is_active=True
    ).first()
    if parameter_type is None:
        raise ProductionQCError("Pick a parameter type.", "parameter_type_id")

    parameters = _parameters_of(parameter_type)
    _check_readings(
        [(p.id, p.parameter_name, p.is_mandatory) for p in parameters], readings
    )

    now = timezone.now()
    entry = ProductionQCEntry.objects.create(
        company=company,
        production_run_id=line.run_id,
        line_id=line.line_id,
        item_code=line.item_code,
        product=line.product,
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
    """Correct a check that is waiting for approval or was sent back, and send it again."""
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
